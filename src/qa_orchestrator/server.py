from pathlib import Path
from typing import Annotated, Literal

from fastmcp import FastMCP
from pydantic import Field

from qa_orchestrator.config import Settings
from qa_orchestrator.contracts import (
    FinalReviewReport,
    ProfileReviewReport,
    QaOrchestrationCatalog,
    QaTaskOutcome,
    QaTaskType,
    ReviewAgent,
    ReviewBundle,
    ReviewContext,
    ReviewRoute,
    ReviewRouteBatch,
)
from qa_orchestrator.git_evidence import resolve_git_input
from qa_orchestrator.model_policy import ModelPolicyResponse
from qa_orchestrator.orchestration import (
    AdvanceQaOrchestrationRequest,
    DeepReviewSignals,
    QaOrchestrationDeletion,
    QaOrchestrationList,
    QaOrchestrationSession,
)
from qa_orchestrator.service import OrchestratorService, QaOrchestrationAdvanceResult

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
    "For local Git branches or refs, resolve_git_review_input can pin both commits and their exact diff metadata. "
    "Before calling start_qa_orchestration for ordinary_review or widget_review, complete review-context "
    "preflight: resolve the authoritative task/MR and repository; for Git input pin exact base_sha and head_sha; "
    "for an MR diff use input_kind=diff with no Git revisions and bind its exact hash and hunks; inspect the diff "
    "and changed paths, build the compact Evidence Packet and stable evidence_refs, compute diff_sha256 and "
    "evidence_packet_sha256, and include input_manifest.schema_version=2 with source_inventory_status and "
    "source_conflict_status. A task/MR URL alone is not enough. Keep raw source, diff, and packet content with "
    "the host; send only identifiers, digests, and metadata. If repository, required Git revisions, diff, or packet identity "
    "cannot be pinned, do not start; resolve it or tell the user what is missing. Record unavailable requirement/spec "
    "sources with partial or unknown manifest statuses; these gaps need not prevent review once the code input identity "
    "is pinned. "
    "For a narrow, single-concern, low-risk change, use one profile; use a bundle for broad, "
    "cross-concern, or high-risk work. Several changed files alone do not require a bundle. "
    "After selecting a route at triage, use selected_review_routes from the advance response; it "
    "contains the exact checklists and execution_groups, so no separate preparation call is needed. "
    "The prepare tools remain available for standalone lookup or recovery. Run groups in order; profiles in the same group may run "
    "concurrently when the host supports parallel workers, otherwise run them sequentially. "
    "For every finding-producing profile, the host runs an independent second review and a "
    "separate verifier before reporting the profile complete; preserve incomplete passes "
    "as gaps and keep final decisions host-owned. The MCP server does not execute these calls. "
    "Wait for the whole group, then report its ordered completed_profiles together in one call. "
    "The legacy completed_profile field remains available for one-at-a-time reporting. "
    "Skip the separate synthesis step only for a clean single-profile result; the host still owns and "
    "produces the final QA outcome. "
    "Follow the declared execution_groups and report completed_profiles (or the legacy "
    "completed_profile field) and risk_signals accurately. "
    "The orchestrator validates the reported profile order; it cannot confirm each review ran, "
    "whether profiles ran concurrently, or whether host-reported risk_signals reflect evidence. "
    "Risk signals require a rationale and Evidence Packet references, which are retained with the session. "
    "Bound reviews validate report/profile alignment, citation membership, and finding-source lineage, "
    "not report truth. File-based recommended_profiles are hints; the host selects the route. "
    "In host_default model-policy mode, use the model and reasoning configured by the host. "
    "Full workflow guidance remains in client-rules/generic/QA_ORCHESTRATOR_INSTRUCTIONS.md."
)


def build_server(service: OrchestratorService) -> FastMCP:
    mcp = FastMCP(name="qa-orchestrator", instructions=SERVER_INSTRUCTIONS)

    @mcp.tool(title="Resolve local Git review input", annotations=READ_ONLY_TOOL_ANNOTATIONS)
    def resolve_git_review_input(
        repository: Annotated[str, Field(description="Absolute path to the local Git repository on the server host.")],
        base: Annotated[str, Field(description="Locally available base Git ref or commit.")],
        head: Annotated[str, Field(description="Locally available head Git ref or commit.")],
    ) -> dict[str, object]:
        """Resolve two locally available Git refs into immutable commits and diff metadata.

        The repository path is on the server host. This compares the resolved commits directly;
        choose the merge base explicitly when a merge-base comparison is required. It does not
        fetch refs, inspect uncommitted changes, or construct the evidence packet.
        """
        return resolve_git_input(Path(repository), base, head)

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

        Use before reviewing one scoped concern when no session is active, or to recover a checklist
        when the triage response is unavailable; no session is required.
        - Standalone review: use get_qa_orchestration_catalog() to match profile focus to
          changed files and confirmed stack.
        - Session primary_review: use selected_review_routes returned by the successful triage
          advance call. It contains every selected checklist and execution_group; do not make a
          separate preparation call for the same session.

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

        Use for standalone bundle lookup or to recover checklists when the successful triage
        response is unavailable. In a session, the triage advance response includes the selected
        checklists and execution_groups. Run execution_groups in order;
        profiles in one group are independent and may run concurrently when the host supports it.
        After every profile in the group finishes, record the complete group once in bundle order
        with completed_profiles. Older hosts may continue using completed_profile one at a time.
        For a single profile, use prepare_qa_orchestration.
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

        - For a session, the successful triage response includes selected_review_routes with the
          selected profile or bundle checklists and execution_groups.
        - Standalone or recovery lookup: prepare_qa_orchestration(agent_profile) or
          prepare_qa_orchestration_bundle(bundle).
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
                    "for automation work, or other. This shapes the task-type shortlist; review_context may "
                    "append stack-matched bundles. The host chooses the route from changed files and evidence; "
                    "this is not a risk level."
                )
            ),
        ],
        review_context: Annotated[
            ReviewContext | None,
            Field(
                description=(
                    "May be omitted only for non-review task types. Required for ordinary_review and widget_review. "
                    "Before calling for either review type, resolve the authoritative task/MR and, for Git input, exact base_sha "
                    "and head_sha; for standalone diff use input_kind=diff with no revisions. Inspect the diff, and provide changed_files, diff_sha256, "
                    "evidence_packet_sha256, evidence_refs, and input_manifest.schema_version=2 with "
                    "source_inventory_status and source_conflict_status. Do not use a URL or placeholder instead. "
                    "Keep raw source, diff, and Evidence Packet contents with the host; pass only identifiers, "
                    "digests, and metadata. "
                    "When supplied, every completed profile must include a structured report whose "
                    "evidence references belong to this packet; completed outcomes require a final report. "
                    "The response may include file-type-based recommended_profiles and stack-matched "
                    "recommended_bundles as non-binding hints."
                    " Optional evidence_registry metadata binds each ref to a source kind, revision, "
                    "coordinates and content hash. The host verifies actual content locally."
                )
            ),
        ] = None,
    ) -> QaOrchestrationSession:
        """Create a QA session and bind code reviews to one immutable review input.

        Before calling for ordinary_review or widget_review, complete review preflight: resolve the authoritative
        task/MR, pin exact base_sha and head_sha for Git input, or use input_kind=diff with no Git revisions for a
        standalone unified diff; inspect the exact diff, build the host-owned Evidence Packet and
        stable refs, compute diff_sha256 and evidence_packet_sha256, and provide input_manifest.schema_version=2
        with source_inventory_status and source_conflict_status. A task or MR URL alone is not enough. If an exact
        repository, required Git revisions, diff, or packet identity cannot be pinned, do not start; resolve it or tell the user
        what is missing. Record unavailable requirement/spec sources with partial or unknown manifest statuses.

        Every call creates a separate session. Use prepare_qa_orchestration for a stateless profile
        checklist. `task_type` narrows the bundle shortlist; the host still selects the review route.
        Code review task types require `review_context` with the exact source and Evidence Packet
        identifiers plus an input manifest schema v2. The server stores identifiers and submitted
        report fields, not the raw diff or full Evidence Packet.

        - Sessions expire after the configured TTL (1800 seconds by default).
        - Capacity is 100 sessions by default and can be configured.
        - Expired sessions are purged and retained terminal sessions may be evicted. If capacity can
          only be freed by removing an active session, the call returns a `session limit` error.
        """
        return service.start_qa_orchestration(
            task_type, review_context, require_review_preflight=True
        )

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
                    "Legacy-compatible single-profile completion. After the complete execution_group "
                    "has finished, submit exactly the current_profile returned by the latest "
                    "advance_qa_orchestration call and follow fixed route order; never advance "
                    "parallel profiles concurrently. Use completed_profiles to record a whole "
                    "group in one call. Omit for other steps or an early stop."
                )
            ),
        ] = None,
        completed_profiles: Annotated[
            list[ReviewAgent] | None,
            Field(
                description=(
                    "After the whole next execution_group has finished, submit every profile in that "
                    "group exactly once and in fixed route order. This records the group in one call. "
                    "For a bundle, the list must match the next complete group; for one profile, "
                    "submit a one-item list. Mutually exclusive with completed_profile. Omit for "
                    "other steps or an early stop."
                )
            ),
        ] = None,
        profile_reports: Annotated[
            list[ProfileReviewReport] | None,
            Field(
                description=(
                    "For a review-context-bound session, one structured result per completed profile, "
                    "in the same order. Every finding must cite Evidence Packet references. Optional "
                    "execution_metrics are host-reported and are not independently verified. Up to "
                    "ten reports are accepted, with 72 KiB per report and 288 KiB combined per session. "
                    "Omit for legacy sessions without review_context."
                )
            ),
        ] = None,
        risk_signals: Annotated[
            DeepReviewSignals | None,
            Field(
                description=(
                    "Required with the final completed primary-review profile. Send an empty object "
                    "when no evidence-based fixed escalation signals apply; any true signal requires "
                    "a short rationale and Evidence Packet references. Do not send on earlier "
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
        request_id: Annotated[
            str | None,
            Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$", description=(
                "Optional unique transition key. Requires expected_revision. Retrying the exact "
                "same payload returns current retained state without repeating the transition; "
                "reusing the key with another payload is rejected."
            )),
        ] = None,
        expected_revision: Annotated[
            int | None,
            Field(ge=0, strict=True, description=(
                "Optional workflow revision from the latest session. A new transition is rejected "
                "if stale; a recognized request_id replay remains safe."
            )),
        ] = None,
        implementation_review: Annotated[
            bool | None,
            Field(description=(
                "At triage, whether the user's request includes review of implementation behavior. "
                "Unknown when omitted. Limited profiles require explicit acknowledgement "
                "when implementation behavior is in scope."
            )),
        ] = None,
        acknowledge_limited_profile: Annotated[
            bool,
            Field(description="At triage, explicitly accept that the selected test analyzer or explorer cannot report implementation findings."),
        ] = False,
        compact_response: Annotated[
            bool, Field(description="Return workflow state without stored context, reports and audit history; use get_qa_orchestration for full state.")
        ] = False,
        stop_reason: Annotated[
            str | None,
            Field(min_length=1, max_length=2000, description="Required for partial or blocked early stops: state the observed cause or explicitly say it is unknown, without secrets or raw private output."),
        ] = None,
    ) -> QaOrchestrationAdvanceResult:
        """Record one active QA step's result; return updated state and next_action instructions for the host.

        After completed triage, the response also includes selected_review_routes with the exact
        selected checklists and execution_groups, avoiding a separate preparation tool call.

        For finalization, use finish_qa_orchestration at awaiting_host_outcome;
        after early stops, match the recorded outcome.

        - Triage: select exactly one bundle or profile; default to one profile for narrow work.
          Set implementation_review=true when implementation behavior is in scope.
        - Every call requires run_id, completed_step, and status. Copy completed_step from the latest
          session's current_step; use status=completed after a successful stage.
        - Primary review: after a whole execution group finishes, report its ordered profiles together
          with completed_profiles. In a review-context-bound session, also supply matching profile_reports.
          Older hosts may continue reporting completed_profile one at a time.
          The final group requires risk_signals ({} if none).
        - Clean single-profile fast path: set skip_synthesis=true only when the route contains one profile
          and that review has no findings,
          no unresolved material questions, and no risk signals. This bypasses only the separate synthesis
          step; the host still validates and produces the final QA report. The server cannot verify this claim.
          Bundles, findings, unresolved questions, or any risk signal require the normal synthesis stage.
        - Trust boundary: completion fields, execution_metrics, and risk_signals are host-reported.
          The server cannot confirm each review ran, whether it ran concurrently, or that
          risk_signals reflect evidence; bound sessions validate report/profile alignment,
          citation membership, finding-source lineage, and risk-signal reference membership,
          not report truth.
        - Early stop: provide stop_reason; omit route selectors, completion fields, and risk_signals.
        - Success saves state and refreshes expires_at.
        - Invalid/stale requests and unknown/expired run_ids fail without advancing.
        - With request_id and expected_revision, exact retries return current retained state without
          another transition or TTL refresh. Conflicting payloads and new stale revisions fail.
        - Legacy calls without request_id are non-idempotent: never retry them blindly. After a lost response or
          transition error, read get_qa_orchestration(run_id) and continue from its current_step;
          if the session is unknown or expired, start a new session.
        """
        if status in ("partial", "blocked") and (stop_reason is None or not stop_reason.strip()):
            raise ValueError("stop_reason is required for partial or blocked early stops")
        result = service.advance_qa_orchestration(
            AdvanceQaOrchestrationRequest(
                run_id=run_id,
                completed_step=completed_step,
                status=status,
                stop_reason=stop_reason,
                selected_bundle=selected_bundle,
                selected_profile=selected_profile,
                completed_profile=completed_profile,
                completed_profiles=completed_profiles,
                profile_reports=profile_reports,
                risk_signals=risk_signals,
                skip_synthesis=skip_synthesis,
                request_id=request_id,
                expected_revision=expected_revision,
                implementation_review=implementation_review,
                acknowledge_limited_profile=acknowledge_limited_profile,
            ),
        )
        result._compact_response = compact_response
        return result

    @mcp.tool(title="Renew active QA session lease", annotations=STATE_TOOL_ANNOTATIONS)
    def renew_qa_orchestration(
        run_id: RunId,
        expected_revision: Annotated[
            int | None,
            Field(ge=0, strict=True, description="Optional current workflow revision; stale renewals fail."),
        ] = None,
    ) -> QaOrchestrationSession:
        """Extend an active session's TTL while host review work is running.

        Renew before expires_at. This changes only the lease, without advancing a profile or
        workflow revision. Active primary/deep/synthesis and awaiting-host-outcome sessions may be
        renewed; terminal or expired sessions cannot be revived. No worker or model is launched.
        """
        return service.renew_qa_orchestration(run_id, expected_revision)

    @mcp.tool(title="Inspect one QA session", annotations=READ_ONLY_TOOL_ANNOTATIONS)
    def get_qa_orchestration(run_id: RunId) -> QaOrchestrationSession:
        """Read one retained run_id's state, audit trace, bound context and structured reports.

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
        """List non-expired QA session identifiers and brief metadata, including bound review identity.

        Recover a lost run_id by matching task type, route, and stage; confirm if several match.
        Call get_qa_orchestration(run_id) for full state and next action, or
        get_qa_orchestration_model_policy() for server policy. Takes no arguments.

        - Includes retained terminal sessions; expired or evicted sessions are unrecoverable.
        - Sorts by expires_at descending, then run_id descending; empty means none are retained.
        - After restart, unfinished sessions and finalized sessions with saved review artifacts
          restored from configured local storage appear.
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
        final_review_report: Annotated[
            FinalReviewReport | None,
            Field(
                description=(
                    "Structured synthesized findings and coverage for a review_context-bound session. "
                    "Required when its outcome is completed; each finding cites registered evidence refs "
                    "and the original profile-local candidate IDs and evidence that support it. The report "
                    "must include one candidate_decisions entry per submitted candidate. Every defect "
                    "candidate needs failure_condition, flip_condition, expected_behavior, and "
                    "actual_behavior (source-predicted, not runtime proof), and every outcome needs a "
                    "bound refutation assessment: not_refuted for confirmed/duplicate, refuted for "
                    "rejected, unverified for unverified. Rejected/unverified outcomes preserve the original "
                    "candidate snapshot. Confirmed findings "
                    "require host severity, impact, affected area, and source locations. Optional "
                    "verification_records capture test/CI/runtime scenarios, results, environment, run "
                    "reference, reason, and head SHA when known; for diff-only reviews the head binding "
                    "remains unknown. These records remain host-supplied and "
                    "are not independently authenticated by the orchestrator. The report "
                    "is limited to 80 KiB; profile plus final reports are limited to 288 KiB per session."
                )
            ),
        ] = None,
        expected_revision: Annotated[
            int | None,
            Field(ge=0, strict=True, description=(
                "Optional current workflow revision for a new finalization. The same retained "
                "outcome/report can still be replayed idempotently."
            )),
        ] = None,
    ) -> QaOrchestrationSession:
        """Finalize the QA run and store its structured report when the session is review-bound.

        After normal synthesis or an eligible clean single-profile fast path, use the retained
        run_id at awaiting_host_outcome to choose completed, partial, or blocked.
        After an early stop recorded by advance_qa_orchestration, outcome must match its partial/blocked
        status.

        - A completed review-bound session requires `final_review_report`.
        - Structured reports survive a restart only when the same session store is configured;
          otherwise they are in-memory only. They remain until TTL expiry, capacity eviction, or
          explicit deletion.
        - Same outcome: returns the retained terminal session.
        - Different outcome: `conflicting final outcome`; status unchanged.
        - Premature call: `outcome is not ready`.
        Finalized sessions with review artifacts can be restored from the configured store and
        expire at configured TTL (1800s default), or are removed by eviction or explicit deletion.
        Legacy finalized sessions without review artifacts are removed at restart. Removed run_ids
        cannot be deduplicated.
        """
        return service.finish_qa_orchestration(
            outcome=outcome,
            run_id=run_id,
            final_review_report=final_review_report,
            expected_revision=expected_revision,
        )

    return mcp


def main() -> None:
    settings = Settings.from_env()
    service = OrchestratorService(settings)
    # FastMCP's banner checks PyPI and writes a version cache by default.
    build_server(service).run(show_banner=False)
