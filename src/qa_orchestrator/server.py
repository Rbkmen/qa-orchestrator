from typing import Annotated, Literal

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
FINALIZE_TOOL_ANNOTATIONS = {
    **STATE_TOOL_ANNOTATIONS,
    "idempotentHint": True,
}
RunId = Annotated[
    str,
    Field(
        pattern=r"^qar-[0-9a-f]{32}$",
        description=(
            "Session identifier returned by start_qa_orchestration: qar- followed by "
            "32 lowercase hexadecimal characters. Reuse it for this session; do not invent or transform it."
        ),
    ),
]
CompletableStep = Literal["triage", "primary_review", "deep_review", "synthesis"]


def build_server(service: OrchestratorService) -> FastMCP:
    mcp = FastMCP(name="qa-orchestrator")

    @mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
    def prepare_review_route(
        agent_profile: Annotated[
            ReviewAgent,
            Field(
                description=(
                    "Choose one fixed specialist profile: code_explorer maps execution and evidence; "
                    "code_reviewer reviews implementation; pr_test_analyzer reviews coverage; "
                    "security_reviewer and silent_failure_hunter target security and silent failures; "
                    "ruby_reviewer, python_reviewer, typescript_reviewer, react_reviewer, and "
                    "mobile_reviewer target their named stacks. Choose from changed files and confirmed stack."
                )
            ),
        ],
    ) -> ReviewRoute:
        """Return the fixed focus, constraints, and required output sections for one review profile.

        Use this stateless tool for a single-profile review or a selected bundle member's checklist.
        It does not inspect repository content, run the review, or create or change a session. For a
        tracked multi-stage review, use start_qa_orchestration and advance_qa_orchestration.
        """
        return service.prepare_review_route(agent_profile)

    @mcp.tool(annotations=STATE_TOOL_ANNOTATIONS)
    def start_qa_orchestration(
        task_type: Annotated[
            QaTaskType,
            Field(
                description=(
                    "Broad request category: ordinary_review for implementation review, widget_review "
                    "for widget changes, epic_analysis for broad exploration, requirements_analysis "
                    "for requirement review, qa_planning for coverage planning, autotest_implementation "
                    "for automation work, or other. This affects recommended_bundles only; the host "
                    "chooses the route from changed files and evidence; it is not a risk level."
                )
            ),
        ],
    ) -> QaOrchestrationSession:
        """Create a content-free QA session for a tracked review; call once before triage.

        Every call creates a separate session. Use prepare_review_route for a stateless profile
        checklist. `task_type` selects only the recommended-bundle shortlist; the host selects the
        route from changed files and evidence, and `task_type` is not a risk level.

        - Sessions expire after the configured TTL (1800 seconds by default).
        - Capacity is 100 sessions by default and can be configured.
        - Expired sessions are purged and retained terminal sessions may be evicted. If capacity can
          only be freed by removing an active session, the call returns a `session limit` error.
        """
        return service.start_qa_orchestration(task_type)

    @mcp.tool(annotations=STATE_TOOL_ANNOTATIONS)
    def advance_qa_orchestration(
        run_id: RunId,
        completed_step: Annotated[
            CompletableStep,
            Field(
                description=(
                    "Active step returned in current_step and completed by this call: triage, "
                    "primary_review, deep_review, or synthesis. When current_step is "
                    "awaiting_host_outcome, call finish_qa_orchestration instead."
                )
            ),
        ],
        status: Annotated[
            QaTaskOutcome,
            Field(
                description=(
                    "Use completed after the active step succeeds. Use partial or blocked to stop early; "
                    "then finalize with the same outcome using finish_qa_orchestration."
                )
            ),
        ],
        selected_bundle: Annotated[
            ReviewBundle | None,
            Field(
                description=(
                    "For completed triage, choose one allowed fixed bundle for a broad or cross-concern "
                    "review. It is mutually exclusive with selected_profile; recommended_bundles is only a shortlist."
                )
            ),
        ] = None,
        selected_profile: Annotated[
            ReviewAgent | None,
            Field(
                description=(
                    "For completed triage, choose one allowed profile for a narrow, low-risk review. "
                    "It is mutually exclusive with selected_bundle."
                )
            ),
        ] = None,
        completed_profile: Annotated[
            ReviewAgent | None,
            Field(
                description=(
                    "After each successful primary-review step, submit exactly the current_profile "
                    "returned by the previous advance_qa_orchestration call. Omit for other steps or an early stop."
                )
            ),
        ] = None,
        risk_signals: Annotated[
            DeepReviewSignals | None,
            Field(
                description=(
                    "Required with the final completed primary-review profile. Send an empty object "
                    "when no evidence-based fixed escalation signals apply; do not send on earlier "
                    "profiles or on the synthesis transition."
                )
            ),
        ] = None,
    ) -> QaOrchestrationSession:
        """Complete the active step of a run and return its updated state and next action.

        Pass the session's current_step as completed_step; out-of-order transitions are rejected. If a
        successful response is lost, use get_qa_orchestration to inspect the new state before retrying:
        replaying the previous step is rejected.

        - For triage with status completed, supply exactly one of selected_bundle or selected_profile;
          when stopping with status partial or blocked, omit both.
        - For each completed primary review step, send its current_profile as completed_profile; omit it
          on an early stop. With the final completed primary review role, also send risk_signals (use {}
          when none apply).
        - For an early stop, finalize with finish_qa_orchestration using the same partial or blocked
          status. After synthesis, call finish_qa_orchestration directly.
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
            )
        )

    @mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
    def get_qa_orchestration(run_id: RunId) -> QaOrchestrationSession:
        """Read a session's current content-free state without changing it.

        Use the run_id returned by start_qa_orchestration to resume or recover its current step,
        next action, and model policy. Reads do not extend the session TTL; an expired or unknown id
        returns an error. Use advance_qa_orchestration to change an active run or finish_qa_orchestration
        to record its final outcome.
        """
        return service.get_qa_orchestration(run_id)

    @mcp.tool(annotations=FINALIZE_TOOL_ANNOTATIONS)
    def finish_qa_orchestration(
        run_id: RunId,
        outcome: Annotated[
            QaTaskOutcome,
            Field(
                description=(
                    "Host-selected final QA outcome: completed after synthesis, or the same partial or "
                    "blocked outcome already used for an early stop. Conflicting final outcomes are rejected."
                )
            ),
        ],
    ) -> QaOrchestrationSession:
        """Record the host's final outcome after synthesis or an early stop.

        Call when advance_qa_orchestration reaches `awaiting_host_outcome`, or finalize an early stop
        with the same `partial` or `blocked` outcome.

        - Repeating the same outcome is idempotent while the session is retained; conflicting outcomes
          are rejected.
        - Retention ends when the configured TTL expires (1800 seconds by default), a terminal session
          is evicted to free capacity, or the service restarts. After removal, the run_id is unavailable
          and the outcome can no longer be deduplicated.

        This tool records the host's decision; it does not make it.
        """
        return service.finish_qa_orchestration(
            outcome=outcome,
            run_id=run_id,
        )

    return mcp


def main() -> None:
    settings = Settings.from_env()
    service = OrchestratorService(settings)
    build_server(service).run()
