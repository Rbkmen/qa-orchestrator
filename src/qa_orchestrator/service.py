import hashlib
import json
from pathlib import Path
from threading import RLock

from qa_orchestrator.config import Settings
from qa_orchestrator.contracts import (
    QaTaskOutcome,
    QaTaskOutcomeReceipt,
    QaTaskType,
    ReviewAgent,
    ReviewRoute,
)
from qa_orchestrator.events import (
    EventSink,
    JsonEventSink,
    qa_task_metric_errors,
    valid_qa_task_metrics,
)
from qa_orchestrator.model_policy import load_model_selection
from qa_orchestrator.orchestration import (
    AdvanceQaOrchestrationRequest,
    OrchestrationStatus,
    QaOrchestrationSession,
    QaOrchestrator,
)
from qa_orchestrator.review_profiles import build_review_route


class OrchestratorService:
    @classmethod
    def from_settings(cls, *, data_dir: Path | None = None) -> "OrchestratorService":
        settings = Settings(data_dir=data_dir) if data_dir is not None else Settings()
        return cls(settings)

    def __init__(self, settings: Settings, events: EventSink | None = None) -> None:
        self.settings = settings
        self.events = events or JsonEventSink(
            settings.metrics_path,
            settings.metrics_retention_days,
            settings.metrics_max_events,
        )
        model_selection = load_model_selection(settings.model_policy_path)
        self.orchestrator = QaOrchestrator(
            ttl_seconds=settings.orchestration_session_ttl_seconds,
            max_sessions=settings.orchestration_max_sessions,
            model_selection=model_selection,
        )
        self._outcome_lock = RLock()

    def prepare_review_route(self, agent_profile: ReviewAgent | str) -> ReviewRoute:
        try:
            resolved_profile = ReviewAgent(agent_profile)
        except (TypeError, ValueError) as exc:
            raise ValueError("unknown review agent profile") from exc
        return build_review_route(resolved_profile)

    def start_qa_orchestration(self, task_type: QaTaskType) -> QaOrchestrationSession:
        return self.orchestrator.start(task_type)

    def advance_qa_orchestration(
        self,
        request: AdvanceQaOrchestrationRequest,
    ) -> QaOrchestrationSession:
        return self.orchestrator.advance(**request.model_dump())

    def get_qa_orchestration(self, run_id: str) -> QaOrchestrationSession:
        return self.orchestrator.get(run_id)

    def record_qa_task_outcome(
        self,
        *,
        task_type: QaTaskType,
        outcome: QaTaskOutcome,
        codegraph_calls: int,
        source_mcp_calls: int,
        findings_identified: int,
        findings_confirmed: int,
        findings_rejected: int,
        repeated_source_reads: int,
        deep_analysis_used: bool = False,
        deep_model: str | None = None,
        deep_reasoning: str | None = None,
        deep_duration_ms: int | None = None,
        deep_input_tokens: int | None = None,
        deep_output_tokens: int | None = None,
        deep_findings_identified: int | None = None,
        deep_findings_new_confirmed: int | None = None,
        deep_findings_rejected: int | None = None,
        triage_input_tokens: int | None = None,
        triage_output_tokens: int | None = None,
        primary_review_input_tokens: int | None = None,
        primary_review_output_tokens: int | None = None,
        synthesis_input_tokens: int | None = None,
        synthesis_output_tokens: int | None = None,
        evidence_packet_tokens: int | None = None,
        merge_requests_count: int | None = None,
        repositories_count: int | None = None,
        codegraph_response_tokens: int | None = None,
        source_mcp_response_tokens: int | None = None,
        avoided_source_read_tokens: int | None = None,
        orchestration_used: bool | None = None,
        triage_calls: int = 0,
        primary_review_calls: int = 0,
        deep_review_calls: int = 0,
        synthesis_calls: int = 0,
        orchestration_steps_completed: int = 0,
        orchestration_retries: int = 0,
        run_id: str | None = None,
    ) -> QaTaskOutcomeReceipt:
        if orchestration_used is None:
            orchestration_used = run_id is not None
        event: dict[str, object] = {
            "schema_version": 2,
            "task_type": task_type,
            "outcome": outcome,
            "deep_analysis_used": deep_analysis_used,
            "findings_identified": findings_identified,
            "findings_confirmed": findings_confirmed,
            "findings_rejected": findings_rejected,
            "codegraph_calls": codegraph_calls,
            "source_mcp_calls": source_mcp_calls,
            "repeated_source_reads": repeated_source_reads,
            "orchestration_used": orchestration_used,
        }
        for field, value in (
            ("deep_model", deep_model),
            ("deep_reasoning", deep_reasoning),
            ("deep_duration_ms", deep_duration_ms),
            ("deep_input_tokens", deep_input_tokens),
            ("deep_output_tokens", deep_output_tokens),
            ("deep_findings_identified", deep_findings_identified),
            ("deep_findings_new_confirmed", deep_findings_new_confirmed),
            ("deep_findings_rejected", deep_findings_rejected),
            ("triage_input_tokens", triage_input_tokens),
            ("triage_output_tokens", triage_output_tokens),
            ("primary_review_input_tokens", primary_review_input_tokens),
            ("primary_review_output_tokens", primary_review_output_tokens),
            ("synthesis_input_tokens", synthesis_input_tokens),
            ("synthesis_output_tokens", synthesis_output_tokens),
            ("evidence_packet_tokens", evidence_packet_tokens),
            ("merge_requests_count", merge_requests_count),
            ("repositories_count", repositories_count),
            ("codegraph_response_tokens", codegraph_response_tokens),
            ("source_mcp_response_tokens", source_mcp_response_tokens),
            ("avoided_source_read_tokens", avoided_source_read_tokens),
        ):
            if value is not None:
                event[field] = value
        for field, value in (
            ("triage_calls", triage_calls),
            ("primary_review_calls", primary_review_calls),
            ("deep_review_calls", deep_review_calls),
            ("synthesis_calls", synthesis_calls),
            ("orchestration_steps_completed", orchestration_steps_completed),
            ("orchestration_retries", orchestration_retries),
        ):
            if orchestration_used or value != 0:
                event[field] = value
        if not valid_qa_task_metrics(event):
            raise ValueError(
                "QA task metrics are inconsistent: "
                + "; ".join(qa_task_metric_errors(event))
            )
        if orchestration_used and run_id is None:
            raise ValueError("run_id is required when orchestration_used is true")
        if run_id is not None and not orchestration_used:
            raise ValueError("run_id requires orchestration_used")

        with self._outcome_lock:
            if run_id is not None:
                session = self.orchestrator.get(run_id)
                deep_recommended, deep_reasons = self._deep_escalation_metrics(session)
                event["deep_escalation_recommended"] = deep_recommended
                event["deep_escalation_reason_codes"] = deep_reasons
                if event["task_type"] != session.task_type:
                    raise ValueError("task_type does not match run_id")
            if not valid_qa_task_metrics(event):
                raise ValueError(
                    "QA task metrics are inconsistent: "
                    + "; ".join(qa_task_metric_errors(event))
                )
            fingerprint = _outcome_fingerprint(event)
            if run_id is not None:
                orchestration_errors = self._orchestration_metric_errors(
                    event, session, outcome
                )
                if orchestration_errors:
                    raise ValueError(
                        "QA task metrics are inconsistent: "
                        + "; ".join(orchestration_errors)
                    )
                if session.status in {
                    OrchestrationStatus.COMPLETED,
                    OrchestrationStatus.PARTIAL,
                    OrchestrationStatus.BLOCKED,
                }:
                    if session.status.value != outcome:
                        raise ValueError("conflicting final outcome")
                    if session.outcome_recorded:
                        if self.orchestrator.get_outcome_fingerprint(run_id) != fingerprint:
                            raise ValueError("conflicting outcome payload")
                        return QaTaskOutcomeReceipt(status="recorded")
                self.orchestrator.finish(run_id=run_id, outcome=outcome)

            receipt = self.events.record_qa_task_outcome(event)
            if receipt.status == "recorded" and run_id is not None:
                self.orchestrator.mark_outcome_recorded(
                    run_id=run_id,
                    outcome=outcome,
                    fingerprint=fingerprint,
                )
            return receipt

    def _orchestration_metric_errors(
        self,
        event: dict[str, object],
        session: QaOrchestrationSession,
        outcome: QaTaskOutcome,
    ) -> list[str]:
        errors: list[str] = []
        expected_recommended, expected_reasons = OrchestratorService._deep_escalation_metrics(session)
        if (
            event.get("deep_escalation_recommended") is not expected_recommended
            or event.get("deep_escalation_reason_codes") != expected_reasons
        ):
            errors.append("deep escalation metadata does not match the orchestration state")
        deep_branch_selected = session.deep_reason_code is not None
        deep_review_was_used = event["deep_review_calls"] > 0
        if deep_review_was_used and not deep_branch_selected:
            errors.append("deep_review_calls does not match the deep-review branch")
        if deep_review_was_used and (
            event.get("deep_model") != self.orchestrator.model_selection.deep_model
            or event.get("deep_reasoning") != self.orchestrator.model_selection.deep_reasoning
        ):
            errors.append("deep_model and deep_reasoning must describe the configured deep stage")
        if outcome != "completed":
            return errors

        required_primary_calls = len(session.review_profiles)
        required_steps = len(session.review_profiles) + 2 + int(deep_branch_selected)
        if event["deep_analysis_used"] is not deep_branch_selected:
            errors.append(f"deep_analysis_used must be {deep_branch_selected}")
        if event["triage_calls"] < 1:
            errors.append("triage_calls must be >= 1")
        if event["primary_review_calls"] < required_primary_calls:
            errors.append(f"primary_review_calls must be >= {required_primary_calls}")
        if event["synthesis_calls"] < 1:
            errors.append("synthesis_calls must be >= 1")
        if event["deep_review_calls"] < int(deep_branch_selected):
            errors.append(f"deep_review_calls must be >= {int(deep_branch_selected)}")
        if event["orchestration_steps_completed"] < required_steps:
            errors.append(f"orchestration_steps_completed must be >= {required_steps}")
        return errors

    @staticmethod
    def _deep_escalation_metrics(
        session: QaOrchestrationSession,
    ) -> tuple[bool, list[str]]:
        if session.deep_assessment is not None:
            return (
                session.deep_assessment.should_escalate,
                [reason.value for reason in session.deep_assessment.reason_codes],
            )
        if session.deep_reason_code is not None:
            return True, [session.deep_reason_code.value]
        return False, []


def _outcome_fingerprint(event: dict[str, object]) -> str:
    payload = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
