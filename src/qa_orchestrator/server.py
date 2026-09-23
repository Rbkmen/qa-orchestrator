import sys
from typing import Annotated

from fastmcp import FastMCP
from pydantic import AfterValidator, Field

from qa_orchestrator.config import Settings
from qa_orchestrator.contracts import (
    QaTaskOutcome,
    QaTaskOutcomeReceipt,
    QaTaskType,
    ReviewAgent,
    ReviewBundle,
    ReviewRoute,
)
from qa_orchestrator.events import JsonEventSink, read_metrics_lines
from qa_orchestrator.orchestration import (
    AdvanceQaOrchestrationRequest,
    DeepReviewSignals,
    OrchestrationReason,
    OrchestrationStep,
    QaOrchestrationSession,
)
from qa_orchestrator.report import TaskDistributionReport, summarize_events
from qa_orchestrator.service import OrchestratorService

READ_ONLY_TOOL_ANNOTATIONS = {
    "readOnlyHint": True,
    "idempotentHint": True,
    "openWorldHint": False,
}
STATE_TOOL_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": False,
}
RunId = Annotated[str, Field(pattern=r"^qar-[0-9a-f]{32}$")]


def _validate_positive_days(value: int) -> int:
    if value < 1:
        raise ValueError("days must be positive")
    return value


PositiveDays = Annotated[
    int,
    AfterValidator(_validate_positive_days),
    Field(json_schema_extra={"minimum": 1}),
]


def build_server(service: OrchestratorService) -> FastMCP:
    mcp = FastMCP(name="qa-orchestrator")

    @mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
    def prepare_review_route(agent_profile: ReviewAgent) -> ReviewRoute:
        """Return deterministic instructions for one read-only QA review profile."""
        return service.prepare_review_route(agent_profile)

    @mcp.tool(annotations=STATE_TOOL_ANNOTATIONS)
    def start_qa_orchestration(task_type: QaTaskType) -> QaOrchestrationSession:
        """Start a content-free, host-owned QA orchestration session."""
        return service.start_qa_orchestration(task_type)

    @mcp.tool(annotations=STATE_TOOL_ANNOTATIONS)
    def advance_qa_orchestration(
        run_id: RunId,
        completed_step: OrchestrationStep,
        status: QaTaskOutcome,
        selected_bundle: ReviewBundle | None = None,
        selected_profile: ReviewAgent | None = None,
        completed_profile: ReviewAgent | None = None,
        risk_signals: DeepReviewSignals | None = None,
        needs_deep_analysis: Annotated[
            bool,
            Field(description="Legacy compatibility input; prefer risk_signals."),
        ] = False,
        reason_code: Annotated[
            OrchestrationReason | None,
            Field(description="Legacy compatibility input; prefer risk_signals."),
        ] = None,
    ) -> QaOrchestrationSession:
        """Advance one validated, content-free orchestration transition.

        Send risk_signals together with the final completed_profile call of
        primary review, before advancing to synthesis.
        """
        return service.advance_qa_orchestration(
            AdvanceQaOrchestrationRequest(
                run_id=run_id,
                completed_step=completed_step,
                status=status,
                selected_bundle=selected_bundle,
                selected_profile=selected_profile,
                completed_profile=completed_profile,
                risk_signals=risk_signals,
                needs_deep_analysis=needs_deep_analysis,
                reason_code=reason_code,
            )
        )

    @mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
    def get_qa_orchestration(run_id: RunId) -> QaOrchestrationSession:
        """Read the current content-free orchestration state."""
        return service.get_qa_orchestration(run_id)

    @mcp.tool(annotations=STATE_TOOL_ANNOTATIONS)
    def record_qa_task_outcome(
        task_type: QaTaskType,
        outcome: QaTaskOutcome,
        run_id: RunId | None = None,
    ) -> QaTaskOutcomeReceipt:
        """Record only the task category for aggregate distribution.

        `outcome` finalizes orchestration when `run_id` is supplied. The outcome
        and run identifier are not written to the distribution record.
        """
        return service.record_qa_task_outcome(
            task_type=task_type,
            outcome=outcome,
            run_id=run_id,
        )

    @mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
    def get_metrics_report(days: PositiveDays = 7) -> TaskDistributionReport:
        """Read aggregate QA task distribution for a positive time window."""
        if days < 1:
            raise ValueError("days must be positive")
        try:
            lines = read_metrics_lines(service.settings.metrics_path)
        except OSError as exc:
            raise RuntimeError("metrics_unavailable") from exc
        return TaskDistributionReport.model_validate(summarize_events(lines, days=days))

    return mcp


def main() -> None:
    settings = Settings.from_env()
    events = JsonEventSink(
        settings.metrics_path,
        settings.metrics_retention_days,
        settings.metrics_max_events,
    )
    if not events.sanitize_existing_records():
        print(
            "Existing task-distribution data could not be sanitized; "
            "reporting remains aggregate-only.",
            file=sys.stderr,
        )
    service = OrchestratorService(
        settings,
        events,
    )
    build_server(service).run()
