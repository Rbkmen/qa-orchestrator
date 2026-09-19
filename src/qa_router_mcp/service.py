from pathlib import Path

from qa_router_mcp.config import Settings
from qa_router_mcp.contracts import (
    QaTaskOutcome,
    QaTaskOutcomeReceipt,
    QaTaskType,
    ReviewAgent,
    ReviewRoute,
)
from qa_router_mcp.events import EventSink, JsonEventSink, valid_qa_task_metrics
from qa_router_mcp.orchestration import (
    AdvanceQaOrchestrationRequest,
    QaOrchestrationSession,
    QaOrchestrator,
)
from qa_router_mcp.review_profiles import build_review_route


class RouterService:
    @classmethod
    def from_settings(cls, *, data_dir: Path | None = None) -> "RouterService":
        settings = Settings(data_dir=data_dir) if data_dir is not None else Settings()
        return cls(settings)

    def __init__(self, settings: Settings, events: EventSink | None = None) -> None:
        self.settings = settings
        self.events = events or JsonEventSink(
            settings.metrics_path,
            settings.metrics_retention_days,
            settings.metrics_max_events,
        )
        self.orchestrator = QaOrchestrator(
            ttl_seconds=settings.orchestration_session_ttl_seconds,
            max_sessions=settings.orchestration_max_sessions,
        )

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
        codegraph_response_tokens: int | None = None,
        source_mcp_response_tokens: int | None = None,
        avoided_source_read_tokens: int | None = None,
    ) -> QaTaskOutcomeReceipt:
        event: dict[str, object] = {
            "task_type": task_type,
            "outcome": outcome,
            "deep_analysis_used": deep_analysis_used,
            "findings_identified": findings_identified,
            "findings_confirmed": findings_confirmed,
            "findings_rejected": findings_rejected,
            "codegraph_calls": codegraph_calls,
            "source_mcp_calls": source_mcp_calls,
            "repeated_source_reads": repeated_source_reads,
        }
        for field, value in (
            ("deep_model", deep_model),
            ("deep_reasoning", deep_reasoning),
            ("deep_duration_ms", deep_duration_ms),
            ("deep_input_tokens", deep_input_tokens),
            ("deep_output_tokens", deep_output_tokens),
            ("codegraph_response_tokens", codegraph_response_tokens),
            ("source_mcp_response_tokens", source_mcp_response_tokens),
            ("avoided_source_read_tokens", avoided_source_read_tokens),
        ):
            if value is not None:
                event[field] = value
        if not valid_qa_task_metrics(event):
            raise ValueError("QA task metrics are inconsistent")
        return self.events.record_qa_task_outcome(event)
