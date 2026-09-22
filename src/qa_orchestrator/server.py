from typing import Annotated, Literal

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
from qa_orchestrator.report import MetricsReport, summarize_events
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
NonNegativeInt = Annotated[int, Field(ge=0)]
SolModel = Literal["gpt-5.6-sol"]
SolReasoning = Literal["high"]


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
        Terra primary review, before advancing to synthesis.
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
        codegraph_calls: NonNegativeInt,
        source_mcp_calls: NonNegativeInt,
        findings_identified: NonNegativeInt,
        findings_confirmed: NonNegativeInt,
        findings_rejected: NonNegativeInt,
        repeated_source_reads: NonNegativeInt,
        deep_analysis_used: bool = False,
        deep_model: SolModel | None = None,
        deep_reasoning: SolReasoning | None = None,
        deep_duration_ms: NonNegativeInt | None = None,
        deep_input_tokens: NonNegativeInt | None = None,
        deep_output_tokens: NonNegativeInt | None = None,
        deep_findings_identified: NonNegativeInt | None = None,
        deep_findings_new_confirmed: NonNegativeInt | None = None,
        deep_findings_rejected: NonNegativeInt | None = None,
        luna_input_tokens: NonNegativeInt | None = None,
        luna_output_tokens: NonNegativeInt | None = None,
        terra_primary_input_tokens: NonNegativeInt | None = None,
        terra_primary_output_tokens: NonNegativeInt | None = None,
        terra_synthesis_input_tokens: NonNegativeInt | None = None,
        terra_synthesis_output_tokens: NonNegativeInt | None = None,
        evidence_packet_tokens: NonNegativeInt | None = None,
        merge_requests_count: NonNegativeInt | None = None,
        repositories_count: NonNegativeInt | None = None,
        codegraph_response_tokens: NonNegativeInt | None = None,
        source_mcp_response_tokens: NonNegativeInt | None = None,
        avoided_source_read_tokens: NonNegativeInt | None = None,
        orchestration_used: bool | None = None,
        luna_calls: NonNegativeInt = 0,
        terra_calls: NonNegativeInt = 0,
        sol_calls: NonNegativeInt = 0,
        orchestration_steps_completed: NonNegativeInt = 0,
        orchestration_retries: NonNegativeInt = 0,
        run_id: RunId | None = None,
    ) -> QaTaskOutcomeReceipt:
        """Record one content-free outcome owned by the host QA agent.

        When run_id is supplied, include the orchestration stage counters. For
        a completed bundle with N Terra profiles, the minimum is one Luna
        call, N+1 Terra calls, and N+2 completed steps, plus one Sol call and
        one additional step when deep review ran.
        """
        return service.record_qa_task_outcome(
            task_type=task_type,
            outcome=outcome,
            codegraph_calls=codegraph_calls,
            source_mcp_calls=source_mcp_calls,
            findings_identified=findings_identified,
            findings_confirmed=findings_confirmed,
            findings_rejected=findings_rejected,
            repeated_source_reads=repeated_source_reads,
            deep_analysis_used=deep_analysis_used,
            deep_model=deep_model,
            deep_reasoning=deep_reasoning,
            deep_duration_ms=deep_duration_ms,
            deep_input_tokens=deep_input_tokens,
            deep_output_tokens=deep_output_tokens,
            deep_findings_identified=deep_findings_identified,
            deep_findings_new_confirmed=deep_findings_new_confirmed,
            deep_findings_rejected=deep_findings_rejected,
            luna_input_tokens=luna_input_tokens,
            luna_output_tokens=luna_output_tokens,
            terra_primary_input_tokens=terra_primary_input_tokens,
            terra_primary_output_tokens=terra_primary_output_tokens,
            terra_synthesis_input_tokens=terra_synthesis_input_tokens,
            terra_synthesis_output_tokens=terra_synthesis_output_tokens,
            evidence_packet_tokens=evidence_packet_tokens,
            merge_requests_count=merge_requests_count,
            repositories_count=repositories_count,
            codegraph_response_tokens=codegraph_response_tokens,
            source_mcp_response_tokens=source_mcp_response_tokens,
            avoided_source_read_tokens=avoided_source_read_tokens,
            orchestration_used=orchestration_used,
            luna_calls=luna_calls,
            terra_calls=terra_calls,
            sol_calls=sol_calls,
            orchestration_steps_completed=orchestration_steps_completed,
            orchestration_retries=orchestration_retries,
            run_id=run_id,
        )

    @mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
    def get_metrics_report(days: PositiveDays = 7) -> MetricsReport:
        """Read content-free QA task metrics for a positive time window."""
        if days < 1:
            raise ValueError("days must be positive")
        try:
            lines = read_metrics_lines(service.settings.metrics_path)
        except OSError as exc:
            raise RuntimeError("metrics_unavailable") from exc
        return MetricsReport.model_validate(summarize_events(lines, days=days))

    return mcp


def main() -> None:
    settings = Settings.from_env()
    service = OrchestratorService(
        settings,
        JsonEventSink(
            settings.metrics_path,
            settings.metrics_retention_days,
            settings.metrics_max_events,
        ),
    )
    build_server(service).run()
