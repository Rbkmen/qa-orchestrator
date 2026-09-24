from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

from qa_orchestrator.config import Settings
from qa_orchestrator.contracts import (
    QaTaskOutcome,
    QaTaskType,
    ReviewAgent,
    ReviewBundle,
    ReviewRoute,
)
from qa_orchestrator.orchestration import (
    AdvanceQaOrchestrationRequest,
    DeepReviewSignals,
    OrchestrationReason,
    OrchestrationStep,
    QaOrchestrationSession,
)
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
        risk_signals: Annotated[
            DeepReviewSignals | None,
            Field(
                description=(
                    "Required with the final completed primary-review profile. "
                    "Send an empty object when no signals apply."
                )
            ),
        ] = None,
        needs_deep_analysis: Annotated[
            bool,
            Field(description="Legacy compatibility input; prefer risk_signals."),
        ] = False,
        reason_code: Annotated[
            OrchestrationReason | None,
            Field(description="Legacy compatibility input; prefer risk_signals."),
        ] = None,
    ) -> QaOrchestrationSession:
        """Require risk_signals when completing the final primary review profile.

        Send an empty object when no signals apply. The legacy manual
        deep-review input remains accepted.
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
    def finish_qa_orchestration(
        run_id: RunId,
        outcome: QaTaskOutcome,
    ) -> QaOrchestrationSession:
        """Finalize a host-owned QA outcome in the orchestration session."""
        return service.finish_qa_orchestration(
            outcome=outcome,
            run_id=run_id,
        )

    return mcp


def main() -> None:
    settings = Settings.from_env()
    service = OrchestratorService(settings)
    build_server(service).run()
