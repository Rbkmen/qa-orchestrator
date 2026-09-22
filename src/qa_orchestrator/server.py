from fastmcp import FastMCP

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
from qa_orchestrator.report import summarize_events
from qa_orchestrator.service import OrchestratorService


def build_server(service: OrchestratorService) -> FastMCP:
    mcp = FastMCP(name="qa-orchestrator")

    @mcp.tool
    def prepare_review_route(agent_profile: ReviewAgent) -> ReviewRoute:
        """Return deterministic instructions for one read-only QA review profile."""
        return service.prepare_review_route(agent_profile)

    @mcp.tool
    def start_qa_orchestration(task_type: QaTaskType) -> QaOrchestrationSession:
        """Start a content-free, host-owned QA orchestration session."""
        return service.start_qa_orchestration(task_type)

    @mcp.tool
    def advance_qa_orchestration(
        run_id: str,
        completed_step: OrchestrationStep,
        status: QaTaskOutcome,
        selected_bundle: ReviewBundle | None = None,
        selected_profile: ReviewAgent | None = None,
        completed_profile: ReviewAgent | None = None,
        risk_signals: DeepReviewSignals | None = None,
        needs_deep_analysis: bool = False,
        reason_code: OrchestrationReason | None = None,
    ) -> QaOrchestrationSession:
        """Advance one validated, content-free orchestration transition."""
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

    @mcp.tool
    def get_qa_orchestration(run_id: str) -> QaOrchestrationSession:
        """Read the current content-free orchestration state."""
        return service.get_qa_orchestration(run_id)

    @mcp.tool
    def record_qa_task_outcome(
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
        luna_input_tokens: int | None = None,
        luna_output_tokens: int | None = None,
        terra_primary_input_tokens: int | None = None,
        terra_primary_output_tokens: int | None = None,
        terra_synthesis_input_tokens: int | None = None,
        terra_synthesis_output_tokens: int | None = None,
        evidence_packet_tokens: int | None = None,
        merge_requests_count: int | None = None,
        repositories_count: int | None = None,
        codegraph_response_tokens: int | None = None,
        source_mcp_response_tokens: int | None = None,
        avoided_source_read_tokens: int | None = None,
        orchestration_used: bool | None = None,
        luna_calls: int = 0,
        terra_calls: int = 0,
        sol_calls: int = 0,
        orchestration_steps_completed: int = 0,
        orchestration_retries: int = 0,
        run_id: str | None = None,
    ) -> QaTaskOutcomeReceipt:
        """Record one content-free outcome owned by the host QA agent."""
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

    @mcp.tool
    def get_metrics_report(days: int = 7) -> dict[str, object]:
        """Read content-free QA task metrics for a positive time window."""
        if days < 1:
            raise ValueError("days must be positive")
        try:
            lines = read_metrics_lines(service.settings.metrics_path)
        except OSError as exc:
            raise RuntimeError("metrics_unavailable") from exc
        return summarize_events(lines, days=days)

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
