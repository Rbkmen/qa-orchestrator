from typing import Annotated, Literal

from fastmcp import FastMCP
from pydantic import Field

from qa_orchestrator.config import Settings
from qa_orchestrator.contracts import (
    QaOrchestrationCatalog,
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
    QaOrchestrationList,
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
            "Session identifier returned by start_qa_orchestration or list_qa_orchestrations: qar- "
            "followed by 32 lowercase hexadecimal characters. Reuse it for this session; "
            "do not invent or transform it."
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
        """Return one specialist's fixed ReviewRoute checklist: profile, display_name, focus,
        required_sections, constraints, and escalation_signals.

        Use before reviewing one scoped concern; no session is required.
        - Standalone review: use get_qa_orchestration_catalog() to match profile focus to
          changed files and confirmed stack.
        - Session primary_review: pass the latest current_profile as agent_profile;
          preserve review_profiles order for bundles.

        This local lookup reads bundled definitions, without executing reviews or calling models.
        Use advance_qa_orchestration to apply a session route.
        Results depend only on agent_profile. Invalid values fail input validation;
        retry with a listed enum value.
        """
        return service.prepare_review_route(agent_profile)

    @mcp.tool(title="Discover QA profiles and bundles", annotations=READ_ONLY_TOOL_ANNOTATIONS)
    def get_qa_orchestration_catalog() -> QaOrchestrationCatalog:
        """Read the static profile/bundle catalog: available specialists, review scopes, and ordered routes.

        Use before creating a session or choosing its triage route; no session or arguments required.
        Task-type recommendations are shortlists, not restrictions. Choose a bundle for broad work
        or a single profile for a narrow concern using changed files and confirmed stack.

        - Detailed checklist: prepare_qa_orchestration(agent_profile).
        - Create a session: start_qa_orchestration; set its route: advance_qa_orchestration.
        - Session IDs: list_qa_orchestrations().

        Available routes come from the installed server's bundled definitions;
        active sessions and configured models do not change this catalog.
        """
        return service.get_qa_orchestration_catalog()

    @mcp.tool(title="Inspect server model settings", annotations=READ_ONLY_TOOL_ANNOTATIONS)
    def get_qa_orchestration_model_policy() -> ModelSelection:
        """Read server-wide model settings for every stage: provider, model IDs, and reasoning.

        Use to inspect the policy loaded for new sessions; takes no arguments.
        - Existing session state and current-stage policy: get_qa_orchestration(run_id).
        - Create a session with these settings: start_qa_orchestration.

        Reads the loaded in-memory snapshot, not the current policy file; restart the server
        connection after file changes. No session is created and no provider is contacted;
        the result does not verify which model the host executed.
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

    @mcp.tool(title="Record QA step result", annotations=STATE_TOOL_ANNOTATIONS)
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
        """Record one active QA step's result; return updated state and next_action instructions for the host.

        For finalization, use finish_qa_orchestration at awaiting_host_outcome;
        after early stops, match the recorded outcome.

        - Triage: select exactly one bundle or profile.
        - Primary review: follow profile order; the final profile requires risk_signals ({} if none).
        - Early stop: omit route selectors, completed_profile, and risk_signals.
        - Success saves state and refreshes expires_at.
        - Invalid/stale requests and unknown/expired run_ids fail without advancing.
        - Non-idempotent: after a lost response, read get_qa_orchestration(run_id);
          prior-step replay is rejected.
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

    @mcp.tool(title="Inspect one QA session", annotations=READ_ONLY_TOOL_ANNOTATIONS)
    def get_qa_orchestration(run_id: RunId) -> QaOrchestrationSession:
        """Read one retained run_id's content-free state: status, current step, next action,
        and current-stage model policy.

        Use run_id on the server retaining that session to resume or reconcile a lost
        advance_qa_orchestration response. Reads do not extend expires_at. Errors `unknown run_id` or
        `expired session` require a new session via start_qa_orchestration.
        If run_id is lost, use list_qa_orchestrations() to find retained sessions first.

        For all-stage server policy, use get_qa_orchestration_model_policy(); for review progress or
        final outcome, use advance_qa_orchestration or finish_qa_orchestration, respectively.
        Local stdio access requires no additional credentials.
        """
        return service.get_qa_orchestration(run_id)

    @mcp.tool(title="List retained QA sessions", annotations=READ_ONLY_TOOL_ANNOTATIONS)
    def list_qa_orchestrations() -> QaOrchestrationList:
        """List non-expired QA session identifiers and brief metadata retained by this server.

        Recover a lost run_id by matching task type, route, and stage; confirm if several match.
        Call get_qa_orchestration(run_id) for full state and next action, or
        get_qa_orchestration_model_policy() for server policy. Takes no arguments.

        - Includes retained terminal sessions; expired or evicted sessions are unrecoverable.
        - Sorts by expires_at descending, then run_id descending; empty means none are retained.
        - After restart, only unfinished sessions restored from configured local storage appear.
        - Reading does not extend session TTL.
        """
        return service.list_qa_orchestrations()

    @mcp.tool(title="Record final QA outcome", annotations=FINALIZE_TOOL_ANNOTATIONS)
    def finish_qa_orchestration(
        run_id: RunId,
        outcome: Annotated[
            QaTaskOutcome,
            Field(
                description="Host-selected final session status: completed, partial, or blocked."
            ),
        ],
    ) -> QaOrchestrationSession:
        """Finalize the whole QA run's outcome; active step results belong to advance_qa_orchestration.

        After synthesis, use the retained run_id at awaiting_host_outcome to choose completed,
        partial, or blocked.
        After an early stop recorded by advance_qa_orchestration, outcome must match its partial/blocked
        status.

        - Same outcome: returns the retained terminal session.
        - Different outcome: `conflicting final outcome`; status unchanged.
        - Premature call: `outcome is not ready`.
        Finalized sessions expire at configured TTL (1800s default), or are removed by eviction
        or restart. Removed run_ids cannot be deduplicated.
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
