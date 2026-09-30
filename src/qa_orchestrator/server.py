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
from qa_orchestrator.model_policy import ModelSelection
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

    @mcp.tool(title="Read specialist review checklist", annotations=READ_ONLY_TOOL_ANNOTATIONS)
    def prepare_qa_orchestration(
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
        """Return a fixed QA review checklist for one agent_profile: review focus, required sections,
        evidence constraints, and escalation signals.

        Use before reviewing one scoped concern; no session is required. During primary_review, pass
        the latest current_profile as agent_profile; for bundles, follow review_profiles order.

        Do not use it to run reviews or manage sessions. Use start_qa_orchestration to create a session,
        advance_qa_orchestration to select its route, and get_qa_orchestration(run_id) to read its state.
        This local lookup makes no external requests or session changes; local stdio needs no additional
        credentials.
        """
        return service.prepare_review_route(agent_profile)

    @mcp.tool(title="Read server model policy", annotations=READ_ONLY_TOOL_ANNOTATIONS)
    def get_qa_orchestration_model_policy() -> ModelSelection:
        """Read the server-wide model policy for all stages of new QA sessions; takes no run_id.

        Returns the provider, model IDs, and reasoning loaded by this running MCP server.
        For an existing session's current step, status, next action, and current-stage model_policy,
        use get_qa_orchestration(run_id). Use start_qa_orchestration to create a new session
        with this server policy.

        This reads an in-memory snapshot, not the policy file; restart the server connection
        after changing that file. It creates no session, contacts no provider, and does not
        verify which model the host executed.
        """
        return service.get_qa_orchestration_model_policy()

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

        Every call creates a separate session. Use prepare_qa_orchestration for a stateless profile
        checklist. `task_type` narrows the bundle shortlist; the host still selects the review route.

        - Sessions expire after the configured TTL (1800 seconds by default).
        - Capacity is 100 sessions by default and can be configured.
        - Expired sessions are purged and retained terminal sessions may be evicted. If capacity can
          only be freed by removing an active session, the call returns a `session limit` error.
        """
        return service.start_qa_orchestration(task_type)

    @mcp.tool(title="Advance active review step", annotations=STATE_TOOL_ANNOTATIONS)
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
        """Record one active review step's completion or an early stop; return the next action.

        Pass current_step as completed_step only at triage, primary_review, deep_review, or synthesis.
        For awaiting_host_outcome or a partial/blocked early stop, use finish_qa_orchestration;
        match the recorded outcome after an early stop.

        - Completed triage: supply exactly one of selected_bundle or selected_profile.
        - Completed primary review: echo current_profile as completed_profile. The final profile also
          requires risk_signals ({} if none apply); omit signals on earlier profiles.
        - Early stop: omit route selectors, completed_profile, and risk_signals.

        Stale/out-of-order steps, invalid arguments/signals, and unknown/expired run_ids fail without advancing.
        Non-idempotent: after a lost successful response, read get_qa_orchestration before continuing;
        replaying the prior step is rejected.
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

    @mcp.tool(title="Read QA session state", annotations=READ_ONLY_TOOL_ANNOTATIONS)
    def get_qa_orchestration(run_id: RunId) -> QaOrchestrationSession:
        """Read a retained session's content-free QA state and stage model policy.

        Use run_id on the server retaining that session to resume or reconcile a lost
        advance_qa_orchestration response. Reads do not extend expires_at. Errors `unknown run_id` or
        `expired session` require a new session via start_qa_orchestration.

        For all-stage server policy, use get_qa_orchestration_model_policy(); for review progress or
        final outcome, use advance_qa_orchestration or finish_qa_orchestration, respectively.
        Local stdio access requires no additional credentials.
        """
        return service.get_qa_orchestration(run_id)

    @mcp.tool(title="Finalize QA session outcome", annotations=FINALIZE_TOOL_ANNOTATIONS)
    def finish_qa_orchestration(
        run_id: RunId,
        outcome: Annotated[
            QaTaskOutcome,
            Field(
                description="Host-selected final session status: completed, partial, or blocked."
            ),
        ],
    ) -> QaOrchestrationSession:
        """Record the host's final QA session outcome after synthesis or an early stop.

        Use the retained run_id at awaiting_host_outcome to choose completed, partial, or blocked.
        After an early stop recorded by advance_qa_orchestration, outcome must match its partial/blocked
        status. For active review steps, use advance_qa_orchestration.

        - Same outcome: returns the retained terminal session.
        - Different outcome: `conflicting final outcome`; status unchanged.
        - Premature call: `outcome is not ready`.
        Retention ends at configured TTL expiry (1800s default), terminal-session eviction, or restart;
        removed run_ids cannot be deduplicated.
        """
        return service.finish_qa_orchestration(
            outcome=outcome,
            run_id=run_id,
        )

    return mcp


def main() -> None:
    settings = Settings.from_env()
    service = OrchestratorService(settings)
    # FastMCP's banner checks PyPI and writes a version cache by default.
    build_server(service).run(show_banner=False)
