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
    QA_TASK_TYPES,
    EventSink,
    JsonEventSink,
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
        run_id: str | None = None,
    ) -> QaTaskOutcomeReceipt:
        if not isinstance(task_type, str) or task_type not in QA_TASK_TYPES:
            raise ValueError("unknown QA task type")
        if not isinstance(outcome, str) or outcome not in {"completed", "partial", "blocked"}:
            raise ValueError("invalid QA task outcome")

        with self._outcome_lock:
            fingerprint = _outcome_fingerprint({"task_type": task_type, "outcome": outcome})
            if run_id is not None:
                session = self.orchestrator.get(run_id)
                if task_type != session.task_type:
                    raise ValueError("task_type does not match run_id")
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

            receipt = self.events.record_qa_task_outcome({"task_type": task_type})
            if receipt.status == "recorded" and run_id is not None:
                self.orchestrator.mark_outcome_recorded(
                    run_id=run_id,
                    outcome=outcome,
                    fingerprint=fingerprint,
                )
            return receipt


def _outcome_fingerprint(event: dict[str, object]) -> str:
    payload = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
