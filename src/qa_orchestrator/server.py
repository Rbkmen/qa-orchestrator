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
    ReviewRouteBatch,
)
from qa_orchestrator.model_policy import ModelPolicyResponse
from qa_orchestrator.orchestration import (
    AdvanceQaOrchestrationRequest,
    DeepReviewSignals,
    QaOrchestrationDeletion,
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
DELETE_TOOL_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": True,
    "openWorldHint": False,
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


SERVER_INSTRUCTIONS = (
    "These instructions are advisory; the host owns review decisions and model execution. "
    "For a narrow, single-concern, low-risk change, use one profile; use a bundle for broad, "
    "cross-concern, or high-risk work. Several changed files alone do not require a bundle. "
    "For a selected bundle, retrieve its checklists and execution_groups once with "
    "prepare_qa_orchestration_bundle. Run groups in order; profiles in the same group may run "
    "concurrently when the host supports parallel workers, otherwise run them sequentially. "
    "Wait for the whole group, then report each completed_profile one at a time in the fixed profile order. "
    "Skip the separate synthesis step only for a clean single-profile result; the host still owns and "
    "produces the final QA outcome. "
    "Follow the declared execution_groups and report completed_profile and risk_signals accurately. "
    "The orchestrator validates the reported profile order; it cannot confirm each review ran, "
    "whether profiles ran concurrently, or whether host-reported risk_signals reflect evidence. "
    "In host_default model-policy mode, use the model and reasoning configured by the host. "
    "Full workflow guidance remains in client-rules/generic/QA_ORCHESTRATOR_INSTRUCTIONS.md."
)


def build_server(service: OrchestratorService) -> FastMCP:
    mcp = FastMCP(name="qa-orchestrator", instructions=SERVER_INSTRUCTIONS)

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
        - Session primary_review: use the latest current_profile as the profile ID.
          For a bundle, fetch its checklists and execution_groups once with
          prepare_qa_orchestration_bundle; do not repeat this lookup for each profile.

        This local lookup reads bundled definitions, without executing reviews or calling models.
        Use advance_qa_orchestration to apply a session route.
        Results depend only on agent_profile. Invalid values fail input validation;
        retry with a listed enum value.
        """
        return service.prepare_review_route(agent_profile)

    @mcp.tool(title="Read bundle review checklists", annotations=READ_ONLY_TOOL_ANNOTATIONS)
    def prepare_qa_orchestration_bundle(
        bundle: Annotated[
            ReviewBundle,
            Field(
                description=(
                    "Selected fixed multi-profile bundle. The tool returns every checklist and its "
                    "ordered execution_groups. Profiles in the same group may run concurrently; "
                    "for one profile, use prepare_qa_orchestration."
                )
            ),
        ],
    ) -> ReviewRouteBatch:
        """Return every fixed ReviewRoute checklist and execution wave for one bundle.

        Use once after choosing a multi-profile bundle, instead of calling
        prepare_qa_orchestration separately for each profile. Run execution_groups in order;
        profiles in one group are independent and may run concurrently when the host supports it.
        After every profile in the group finishes, record their profiles one at a time in bundle
        order with advance_qa_orchestration. For a single profile, use prepare_qa_orchestration.
        This stateless lookup reads bundled definitions; it does not execute reviews, create a
        session, or call models.
        """
        return service.prepare_review_bundle(bundle)

    @mcp.tool(title="Discover QA profiles and bundles", annotations=READ_ONLY_TOOL_ANNOTATIONS)
    def get_qa_orchestration_catalog() -> QaOrchestrationCatalog:
        """Read the static profile/bundle catalog: specialists, review scopes, and execution schedules.

        Use before creating a session or choosing its triage route; no session or arguments required.
        Task-type recommendations are shortlists, not restrictions. Choose a bundle for broad work
        or a single profile for a narrow concern using changed files and confirmed stack.

        - One-profile checklist: prepare_qa_orchestration(agent_profile).
        - Bundle checklists and execution_groups: prepare_qa_orchestration_bundle(bundle).
        - Create a session: start_qa_orchestration; set its route: advance_qa_orchestration.
        - Session IDs: list_qa_orchestrations().

        Available routes come from the installed server's bundled definitions;
        active sessions and configured models do not change this catalog.
        """
        return service.get_qa_orchestration_catalog()

    @mcp.tool(
        title="Inspect server model settings",
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
        output_schema=ModelPolicyResponse.model_json_schema(),
    )
    def get_qa_orchestration_model_policy() -> ModelPolicyResponse:
        """Read the server-wide model policy used to create new sessions.

        The mode is either host_default (use the host's configured model and reasoning)
        or configured (use the returned per-stage provider, model IDs, and reasoning).

        Use to inspect the policy loaded for new sessions; takes no arguments.
        - Existing session state and current-stage policy: get_qa_orchestration(run_id).
        - Create a session with these settings: start_qa_orchestration.

        Reads the loaded in-memory snapshot, not the current policy file; restart the server
        connection after file changes. No session is created and no provider is contacted;
        the result does not verify which model the host executed.
        """
        return ModelPolicyResponse.from_selection(service.get_qa_orchestration_model_policy())

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
                    "Required: copy the exact current_step from the latest session returned by start, "
                    "advance, or get. Complete only triage, primary_review, deep_review, or synthesis. "
                    "If current_step is awaiting_host_outcome, call finish_qa_orchestration instead."
                )
            ),
        ],
        status: Annotated[
            QaTaskOutcome,
            Field(
                description=(
                    "Required on every call, including successful steps: use completed when the active "
                    "step succeeds. Use partial or blocked only to stop early, then finalize with the "
                    "same outcome using finish_qa_orchestration."
                )
            ),
        ],
        selected_bundle: Annotated[
            ReviewBundle | None,
            Field(
                description=(
                    "For completed triage, choose one fixed bundle only for broad, cross-concern, or high-risk "
                    "work; several changed files alone do not require one. It is mutually exclusive with "
                    "selected_profile; recommended_bundles is only a shortlist."
                )
            ),
        ] = None,
        selected_profile: Annotated[
            ReviewAgent | None,
            Field(
                description=(
                    "For completed triage, default to one allowed profile for a narrow, single-concern, "
                    "low-risk review. "
                    "It is mutually exclusive with selected_bundle."
                )
            ),
        ] = None,
        completed_profile: Annotated[
            ReviewAgent | None,
            Field(
                description=(
                    "After the complete execution_group has finished, submit exactly the current_profile "
                    "returned by the latest advance_qa_orchestration call. Report one profile at a time "
                    "in the fixed route order; never advance parallel profiles concurrently. Omit for "
                    "other steps or an early stop."
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
        skip_synthesis: Annotated[
            bool,
            Field(
                description=(
                    "Optional clean-review fast path. Set true only after one selected profile reports "
                    "no findings or unresolved material questions and every risk signal is false. "
                    "The host still validates and reports the final QA result; the server trusts this "
                    "host claim and cannot verify review content. Bundles, findings, unresolved questions, "
                    "or any risk signal require the normal synthesis stage."
                )
            ),
        ] = False,
    ) -> QaOrchestrationSession:
        """Record one active QA step's result; return updated state and next_action instructions for the host.

        For finalization, use finish_qa_orchestration at awaiting_host_outcome;
        after early stops, match the recorded outcome.

        - Triage: select exactly one bundle or profile; default to one profile for narrow work.
        - Every call requires run_id, completed_step, and status. Copy completed_step from the latest
          session's current_step; use status=completed after a successful stage.
        - Primary review: follow profile order; the final profile requires risk_signals ({} if none).
        - Clean single-profile fast path: set skip_synthesis=true only when that review has no findings,
          no unresolved material questions, and no risk signals. This bypasses only the separate synthesis
          step; the host still validates and produces the final QA report. The server cannot verify this claim.
          Bundles, findings, unresolved questions, or any risk signal require the normal synthesis stage.
        - Trust boundary: completed_profile and risk_signals are host-reported. The server
          validates the reported sequence, but cannot confirm each review ran or that
          risk_signals reflect evidence.
        - Early stop: omit route selectors, completed_profile, and risk_signals.
        - Success saves state and refreshes expires_at.
        - Invalid/stale requests and unknown/expired run_ids fail without advancing.
        - Non-idempotent: never retry or replay an advance call blindly. After a lost response or
          transition error, read get_qa_orchestration(run_id) and continue from its current_step;
          if the session is unknown or expired, start a new session.
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
                skip_synthesis=skip_synthesis,
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

    @mcp.tool(title="Discard QA session state", annotations=DELETE_TOOL_ANNOTATIONS)
    def delete_qa_orchestration(run_id: RunId) -> QaOrchestrationDeletion:
        """Delete one QA session from memory and configured recovery storage.
        To retain a final QA outcome, use finish_qa_orchestration.

        run_id selects one session at any stage; no prior advance or finish required.
        Recover a lost ID via list_qa_orchestrations(), matching task type, route, and stage;
        confirm the target if several entries match.

        - Idempotent: deleted=true confirms absence, including unknown or expired IDs.
        - Irreversible: get/advance/finish cannot use the ID, even after restart.
        - Storage failure: memory stays intact; resolve the error and retry.

        Host tasks and model executions are unaffected.
        """
        return service.delete_qa_orchestration(run_id)

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

        After normal synthesis or an eligible clean single-profile fast path, use the retained
        run_id at awaiting_host_outcome to choose completed, partial, or blocked.
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
