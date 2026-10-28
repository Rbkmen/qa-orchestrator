import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from threading import RLock
from types import MappingProxyType
from typing import Literal
from uuid import uuid4

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from qa_orchestrator.contracts import (
    EvidenceRefId,
    FinalReviewReport,
    ProfileChangedPathCoverage,
    ProfileReviewReport,
    QaTaskOutcome,
    QaTaskType,
    ReviewAgent,
    ReviewBundle,
    ReviewContext,
    ReviewCoverageLedger,
    review_changed_paths_sha256,
)
from qa_orchestrator.evidence import (
    complete_criterion_coverage,
    complete_scenario_coverage,
    profile_input_receipt_verified,
    review_completion_gaps,
    validate_context_selection,
    validate_impact_coverage_context,
    validate_registered_locations,
)
from qa_orchestrator.model_policy import (
    DEFAULT_MODEL_SELECTION,
    HostDefaultModelSelection,
    ModelPolicySelection,
    ModelProvider,
    ReasoningEffort,
    is_valid_model_id,
    reasoning_capabilities_verified,
)
from qa_orchestrator.refutation import validate_refutation_check
from qa_orchestrator.review_profiles import (
    REVIEW_BUNDLES,
    bundle_execution_groups,
    needs_targeted_omission_audit,
    recommended_bundles_for,
    recommended_profiles_for_changed_files,
    triage_review_profiles,
)
from qa_orchestrator.session_store import SqliteSessionStore


class OrchestrationStep(StrEnum):
    TRIAGE = "triage"
    PRIMARY_REVIEW = "primary_review"
    DEEP_REVIEW = "deep_review"
    SYNTHESIS = "synthesis"
    AWAITING_HOST_OUTCOME = "awaiting_host_outcome"


class OrchestrationStatus(StrEnum):
    ACTIVE = "active"
    AWAITING_HOST_OUTCOME = "awaiting_host_outcome"
    COMPLETED = "completed"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    EXPIRED = "expired"


class OrchestrationModel(StrEnum):
    LUNA = "gpt-6-luna"
    SOL = "gpt-6-sol"


class OrchestrationReason(StrEnum):
    EVIDENCE_GAP = "evidence_gap"
    CROSS_REPOSITORY = "cross_repository"
    ROOT_CAUSE = "root_cause"
    HIGH_BLAST_RADIUS = "high_blast_radius"
    HIGH_RISK_DOMAIN = "high_risk_domain"
    EVIDENCE_CONFLICT = "evidence_conflict"
    NON_REPRODUCIBLE = "non_reproducible"


class DeepReviewRule(StrEnum):
    HIGH_RISK_WITH_UNCERTAINTY = "high_risk_with_uncertainty"
    MULTIPLE_COMPLEXITY_SIGNALS = "multiple_complexity_signals"
    CRITICAL_EVIDENCE_CONFLICT = "critical_evidence_conflict"


class DeepReviewSignals(BaseModel):
    """Host-supplied escalation signals with the rationale and refs that support them."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    high_risk_domain: bool = Field(
        default=False,
        description="The change affects a sensitive security, identity, payment, fraud, privacy, or access-control area.",
    )
    evidence_uncertain: bool = Field(
        default=False,
        description=(
            "A specific material conclusion remains uncertain, and a scoped read-only review "
            "could plausibly resolve it. Do not set solely because CI, Stage, or runtime checks "
            "were not run."
        ),
    )
    cross_system_scope: bool = Field(
        default=False,
        description="The relevant behavior crosses repository, service, or system boundaries.",
    )
    multiple_plausible_causes: bool = Field(
        default=False,
        description="More than one plausible cause remains after investigation.",
    )
    evidence_conflict: bool = Field(
        default=False,
        description="Relevant authoritative evidence sources disagree.",
    )
    non_reproducible: bool = Field(
        default=False,
        description="The reported behavior cannot be reproduced under expected conditions.",
    )
    high_blast_radius: bool = Field(
        default=False,
        description="The change could affect many consumers, services, or data records.",
    )
    rationale: str | None = Field(
        default=None,
        max_length=2000,
        description="Short explanation for the enabled signals; required when any signal is true.",
    )
    evidence_refs: tuple[EvidenceRefId, ...] = Field(
        default=(),
        max_length=32,
        description="Evidence Packet references supporting the enabled signals.",
    )

    @model_validator(mode="after")
    def validate_signal_evidence(self) -> "DeepReviewSignals":
        signal_values = self.model_dump(exclude={"rationale", "evidence_refs"})
        any_signal = any(signal_values.values())
        if any_signal and (
            not self.rationale or not self.rationale.strip() or not self.evidence_refs
        ):
            raise ValueError("enabled risk signals require rationale and evidence_refs")
        if not any_signal and (self.rationale is not None or self.evidence_refs):
            raise ValueError("rationale and evidence_refs require an enabled risk signal")
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("risk signal evidence_refs must be unique")
        return self


# Keep headroom for defaulted fields added to each persisted candidate.
_MAX_SESSION_REVIEW_ARTIFACT_BYTES = 288 * 1024


def _review_artifact_payload_size(
    profile_reports: Sequence[ProfileReviewReport],
    final_review_report: FinalReviewReport | None,
) -> int:
    size = sum(len(report.model_dump_json().encode("utf-8")) for report in profile_reports)
    if final_review_report is not None:
        size += len(final_review_report.model_dump_json().encode("utf-8"))
    return size


class DeepReviewAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    should_escalate: bool
    triggered_rules: tuple[DeepReviewRule, ...] = ()
    reason_codes: tuple[OrchestrationReason, ...] = ()
    complexity_signal_count: int = Field(ge=0, le=4)


_COMPLEXITY_SIGNAL_REASONS = (
    ("cross_system_scope", OrchestrationReason.CROSS_REPOSITORY),
    ("multiple_plausible_causes", OrchestrationReason.ROOT_CAUSE),
    ("non_reproducible", OrchestrationReason.NON_REPRODUCIBLE),
    ("high_blast_radius", OrchestrationReason.HIGH_BLAST_RADIUS),
)


def assess_deep_review(signals: DeepReviewSignals) -> DeepReviewAssessment:
    """Apply the fixed deep-review rules to structured host signals."""
    complexity_signal_count = sum(
        getattr(signals, field_name) for field_name, _ in _COMPLEXITY_SIGNAL_REASONS
    )
    triggered_rules: list[DeepReviewRule] = []
    reason_codes: list[OrchestrationReason] = []

    def add_reason(reason: OrchestrationReason) -> None:
        if reason not in reason_codes:
            reason_codes.append(reason)

    if signals.high_risk_domain and signals.evidence_uncertain:
        triggered_rules.append(DeepReviewRule.HIGH_RISK_WITH_UNCERTAINTY)
        add_reason(OrchestrationReason.HIGH_RISK_DOMAIN)
        add_reason(OrchestrationReason.EVIDENCE_GAP)

    if complexity_signal_count >= 2:
        triggered_rules.append(DeepReviewRule.MULTIPLE_COMPLEXITY_SIGNALS)
        for field_name, reason in _COMPLEXITY_SIGNAL_REASONS:
            if getattr(signals, field_name):
                add_reason(reason)

    if signals.evidence_conflict and (
        signals.high_risk_domain or signals.cross_system_scope or signals.high_blast_radius
    ):
        triggered_rules.append(DeepReviewRule.CRITICAL_EVIDENCE_CONFLICT)
        add_reason(OrchestrationReason.EVIDENCE_CONFLICT)
        if signals.high_blast_radius:
            add_reason(OrchestrationReason.HIGH_BLAST_RADIUS)

    return DeepReviewAssessment(
        should_escalate=bool(triggered_rules),
        triggered_rules=tuple(triggered_rules),
        reason_codes=tuple(reason_codes),
        complexity_signal_count=complexity_signal_count,
    )


class ModelPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["configured"] = "configured"
    provider: ModelProvider = ModelProvider.OPENAI
    model: OrchestrationModel | str
    reasoning: ReasoningEffort
    reasoning_capabilities_verified: bool = False

    @field_validator("model")
    @classmethod
    def validate_model_id(cls, value: OrchestrationModel | str) -> OrchestrationModel | str:
        if not is_valid_model_id(str(value)):
            raise ValueError("model must be a safe model identifier")
        return value


class HostDefaultStagePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["host_default"] = "host_default"


StageModelPolicy = ModelPolicy | HostDefaultStagePolicy


def _model_value(model: str) -> OrchestrationModel | str:
    try:
        return OrchestrationModel(model)
    except ValueError:
        return model


def build_model_policies(selection: ModelPolicySelection) -> MappingProxyType:
    if isinstance(selection, HostDefaultModelSelection):
        return MappingProxyType(
            {
                step: HostDefaultStagePolicy()
                for step in (
                    OrchestrationStep.TRIAGE,
                    OrchestrationStep.PRIMARY_REVIEW,
                    OrchestrationStep.DEEP_REVIEW,
                    OrchestrationStep.SYNTHESIS,
                )
            }
        )

    return MappingProxyType(
        {
            OrchestrationStep.TRIAGE: ModelPolicy(
                provider=selection.provider,
                model=_model_value(selection.triage_model),
                reasoning=selection.triage_reasoning,
                reasoning_capabilities_verified=reasoning_capabilities_verified(
                    selection.provider, selection.triage_model
                ),
            ),
            OrchestrationStep.PRIMARY_REVIEW: ModelPolicy(
                provider=selection.provider,
                model=_model_value(selection.primary_model),
                reasoning=selection.primary_reasoning,
                reasoning_capabilities_verified=reasoning_capabilities_verified(
                    selection.provider, selection.primary_model
                ),
            ),
            OrchestrationStep.DEEP_REVIEW: ModelPolicy(
                provider=selection.provider,
                model=_model_value(selection.deep_model),
                reasoning=selection.deep_reasoning,
                reasoning_capabilities_verified=reasoning_capabilities_verified(
                    selection.provider, selection.deep_model
                ),
            ),
            OrchestrationStep.SYNTHESIS: ModelPolicy(
                provider=selection.provider,
                model=_model_value(selection.synthesis_model),
                reasoning=selection.synthesis_reasoning,
                reasoning_capabilities_verified=reasoning_capabilities_verified(
                    selection.provider, selection.synthesis_model
                ),
            ),
        }
    )


MODEL_POLICIES = build_model_policies(DEFAULT_MODEL_SELECTION)


class QaTransitionReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    request_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    applied_revision: int = Field(ge=1)


class QaAuditEvent(BaseModel):
    """Hash-chained record of one host-submitted orchestration transition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(pattern=r"^qar-[0-9a-f]{32}$")
    sequence: int = Field(ge=1)
    event_type: Literal["session_started", "transition_applied", "session_finalized"]
    revision: int = Field(ge=0)
    recorded_at: AwareDatetime
    from_status: OrchestrationStatus | None = None
    to_status: OrchestrationStatus
    from_step: OrchestrationStep | None = None
    to_step: OrchestrationStep
    selected_bundle: ReviewBundle | None = None
    selected_profile: ReviewAgent | None = None
    current_profile: ReviewAgent | None = None
    completed_profiles: tuple[ReviewAgent, ...] = ()
    request_id: str | None = None
    request_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    previous_event_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    event_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def verify_event_hash(self) -> "QaAuditEvent":
        if self.event_type == "session_started":
            if (
                self.revision != 0
                or self.from_status is not None
                or self.from_step is not None
                or self.request_sha256 is None
                or self.to_status is not OrchestrationStatus.ACTIVE
                or self.to_step is not OrchestrationStep.TRIAGE
                or self.selected_bundle is not None
                or self.selected_profile is not None
                or self.current_profile is not None
                or self.completed_profiles
            ):
                raise ValueError("session-start audit event has invalid source state")
        elif (
            self.from_status is None
            or self.from_step is None
            or self.request_sha256 is None
        ):
            raise ValueError("transition audit event is missing source state or request hash")
        elif self.event_type == "session_finalized" and self.to_status not in {
            OrchestrationStatus.COMPLETED,
            OrchestrationStatus.PARTIAL,
            OrchestrationStatus.BLOCKED,
        }:
            raise ValueError("session-finalized audit event has a nonterminal status")
        if self.event_sha256 != _audit_event_sha256(self):
            raise ValueError("QA audit event hash mismatch")
        return self


def _audit_event_sha256(event: QaAuditEvent) -> str:
    payload = event.model_dump(mode="json", exclude={"event_sha256"})
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _stable_json_sha256(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _build_review_coverage_ledger(
    context: ReviewContext,
    reports: Sequence[ProfileReviewReport],
    profiles: Sequence[ReviewAgent],
) -> ReviewCoverageLedger:
    path_indexes = {path: index for index, path in enumerate(context.changed_files)}
    registered_source_paths: set[str] = set()
    unavailable_source_paths: set[str] = set()
    source_refs_by_path: dict[str, set[str]] = {}
    for entry in context.evidence_registry:
        if (
            entry.kind == "source"
            and entry.revision == context.head_sha
            and entry.path in path_indexes
        ):
            registered_source_paths.add(entry.path)
            if entry.source_text_available is True:
                source_refs_by_path.setdefault(entry.path, set()).add(entry.evidence_ref)
            elif entry.source_text_available is False:
                unavailable_source_paths.add(entry.path)

    registered_indexes = tuple(
        index
        for index, path in enumerate(context.changed_files)
        if path in registered_source_paths
    )

    available_indexes = tuple(
        index
        for index, path in enumerate(context.changed_files)
        if path in source_refs_by_path
    )
    unavailable_indexes = tuple(
        index
        for index, path in enumerate(context.changed_files)
        if path in unavailable_source_paths and path not in source_refs_by_path
    )
    reports_by_profile = {report.profile: report for report in reports}
    profile_coverage = []
    for profile in profiles:
        report = reports_by_profile.get(profile)
        selection = report.context_selection if report is not None else None
        input_receipt_verified = (
            profile_input_receipt_verified(context, report) if report is not None else False
        )
        included_refs = (
            set(selection.included_refs)
            if selection is not None and input_receipt_verified
            else set()
        )
        included_indexes = tuple(
            index
            for index, path in enumerate(context.changed_files)
            if source_refs_by_path.get(path, set()) & included_refs
        )
        attested_paths = set(report.reviewed_changed_paths) if report is not None else set()
        attested_indexes = tuple(
            index
            for index, path in enumerate(context.changed_files)
            if path in attested_paths
        )
        profile_coverage.append(
            ProfileChangedPathCoverage(
                profile=profile,
                context_selection_recorded=selection is not None,
                executor_input_receipt_verified=input_receipt_verified,
                source_excerpt_included_path_indexes=included_indexes,
                reviewer_attested_path_indexes=attested_indexes,
                reviewed_diff_hunk_indexes=(
                    report.reviewed_diff_hunk_indexes if report is not None else ()
                ),
                execution_metrics=report.execution_metrics if report is not None else None,
            )
        )

    diff_hunk_count = (
        len(context.diff_hunks)
        if context.diff_hunk_inventory_sha256 is not None
        else None
    )
    reviewed_hunks = {
        index
        for profile in profile_coverage
        if profile.profile is not ReviewAgent.CODE_EXPLORER
        for index in profile.reviewed_diff_hunk_indexes
    }
    unreviewed_hunks = (
        tuple(index for index in range(diff_hunk_count) if index not in reviewed_hunks)
        if diff_hunk_count is not None
        else ()
    )

    return ReviewCoverageLedger(
        changed_paths_sha256=review_changed_paths_sha256(context.changed_files),
        changed_path_count=len(context.changed_files),
        registered_head_source_path_indexes=registered_indexes,
        head_source_excerpt_path_indexes=available_indexes,
        head_source_text_unavailable_path_indexes=unavailable_indexes,
        profiles=tuple(profile_coverage),
        diff_hunk_inventory_sha256=context.diff_hunk_inventory_sha256,
        diff_hunk_count=diff_hunk_count,
        unreviewed_diff_hunk_indexes=unreviewed_hunks,
    )


def _append_audit_event(
    session: "QaOrchestrationSession",
    *,
    event_type: Literal["session_started", "transition_applied", "session_finalized"],
    revision: int,
    recorded_at: datetime,
    from_status: OrchestrationStatus | None,
    from_step: OrchestrationStep | None,
    request_id: str | None = None,
    request_sha256: str | None = None,
) -> "QaOrchestrationSession":
    events = session.audit_events
    event = QaAuditEvent.model_construct(
        run_id=session.run_id,
        sequence=len(events) + 1,
        event_type=event_type,
        revision=revision,
        recorded_at=recorded_at,
        from_status=from_status,
        to_status=session.status,
        from_step=from_step,
        to_step=session.current_step,
        selected_bundle=session.selected_bundle,
        selected_profile=session.selected_profile,
        current_profile=session.current_profile,
        completed_profiles=tuple(session.completed_profiles),
        request_id=request_id,
        request_sha256=request_sha256,
        previous_event_sha256=events[-1].event_sha256 if events else None,
        event_sha256="0" * 64,
    )
    event = event.model_copy(update={"event_sha256": _audit_event_sha256(event)})
    return session.model_copy(update={"audit_events": [*events, event]})


class QaOrchestrationSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(pattern=r"^qar-[0-9a-f]{32}$")
    revision: int = Field(default=0, ge=0, description="Workflow revision for conditional writes.")
    audit_trace_complete: bool = Field(
        default=False,
        description="True when the retained audit trace starts with session creation.",
    )
    audit_events: list[QaAuditEvent] = Field(
        default_factory=list,
        max_length=256,
        description="Bounded hash-chained host-submitted workflow trace; no raw request bodies.",
    )
    transition_receipts: list[QaTransitionReceipt] = Field(
        default_factory=list, max_length=128,
        description="Bounded request hashes for safe replay; no raw transition payloads are stored."
    )
    task_type: QaTaskType
    status: OrchestrationStatus
    current_step: OrchestrationStep
    allowed_profiles: list[ReviewAgent] = Field(default_factory=lambda: list(ReviewAgent))
    allowed_bundles: list[ReviewBundle] = Field(default_factory=lambda: list(REVIEW_BUNDLES))
    recommended_bundles: list[ReviewBundle] = Field(
        default_factory=list,
        description="Task- and changed-file-based shortlists only; the host selects the route.",
    )
    recommended_profiles: list[ReviewAgent] = Field(
        default_factory=list,
        description="File-type-based profile hints only; the host selects the route.",
    )
    selected_bundle: ReviewBundle | None = None
    selected_profile: ReviewAgent | None = None
    review_context: ReviewContext | None = None
    review_completion_mode: Literal["legacy", "strict"] = Field(
        default="legacy",
        description="Legacy preserves restored sessions; new bound sessions require complete inputs and impact coverage.",
    )
    review_profiles: list[ReviewAgent] = Field(default_factory=list)
    current_profile: ReviewAgent | None = None
    completed_profiles: list[ReviewAgent] = Field(default_factory=list)
    profile_reports: list[ProfileReviewReport] = Field(default_factory=list, max_length=11)
    final_review_report: FinalReviewReport | None = None
    risk_signals: DeepReviewSignals | None = None
    deep_assessment: DeepReviewAssessment | None = None
    deep_reason_code: OrchestrationReason | None = None
    model_policy: StageModelPolicy | None = None
    next_action: str = Field(min_length=1)
    read_only: Literal[True] = True
    host_owns_decisions: Literal[True] = True
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def validate_review_artifacts(self) -> "QaOrchestrationSession":
        previous_event_sha256 = None
        previous_event_revision = None
        previous_event = None
        for sequence, event in enumerate(self.audit_events, start=1):
            if event.run_id != self.run_id:
                raise ValueError("QA audit event belongs to a different session")
            if event.sequence != sequence:
                raise ValueError("QA audit event sequence is not contiguous")
            if event.previous_event_sha256 != previous_event_sha256:
                raise ValueError("QA audit event chain is broken")
            if previous_event is not None and (
                event.from_status is not previous_event.to_status
                or event.from_step is not previous_event.to_step
            ):
                raise ValueError("QA audit event source state does not match prior state")
            if event.revision > self.revision:
                raise ValueError("QA audit event cannot exceed the session revision")
            if (
                previous_event_revision is not None
                and event.revision != previous_event_revision + 1
            ):
                raise ValueError("QA audit event revisions are not contiguous")
            previous_event = event
            previous_event_sha256 = event.event_sha256
            previous_event_revision = event.revision
        if self.audit_events:
            if self.audit_trace_complete and (
                self.audit_events[0].event_type != "session_started"
                or self.audit_events[0].revision != 0
                or self.audit_events[0].from_status is not None
                or self.audit_events[0].from_step is not None
            ):
                raise ValueError("complete QA audit trace must begin with session creation")
            latest = self.audit_events[-1]
            if latest.revision != self.revision:
                raise ValueError("latest QA audit event must match the session revision")
            if (
                latest.to_status is not self.status
                or latest.to_step is not self.current_step
                or latest.selected_bundle is not self.selected_bundle
                or latest.selected_profile is not self.selected_profile
                or latest.current_profile is not self.current_profile
                or latest.completed_profiles != tuple(self.completed_profiles)
            ):
                raise ValueError("latest QA audit event does not match current session state")
        if len({r.request_id for r in self.transition_receipts}) != len(self.transition_receipts):
            raise ValueError("transition receipt request IDs must be unique")
        if any(r.applied_revision > self.revision for r in self.transition_receipts):
            raise ValueError("transition receipts cannot exceed the session revision")
        if self.audit_trace_complete and not self.audit_events:
            raise ValueError("complete QA audit trace cannot be empty")
        if self.review_context is None and (self.profile_reports or self.final_review_report):
            raise ValueError("review artifacts require a bound review context")
        if self.review_completion_mode == "strict" and self.review_context is None:
            raise ValueError("strict review completion requires a bound review context")
        if self.profile_reports and tuple(report.profile for report in self.profile_reports) != tuple(
            self.completed_profiles
        ):
            raise ValueError("profile reports must match completed profiles in order")
        if (
            self.final_review_report is not None
            and self.final_review_report.profiles_reviewed != tuple(self.completed_profiles)
        ):
            raise ValueError("final report profiles must match completed profiles in order")
        if (
            self.status is OrchestrationStatus.COMPLETED
            and self.review_context is not None
            and self.final_review_report is None
        ):
            raise ValueError("completed review-bound sessions require a final review report")
        if (
            self.status is OrchestrationStatus.COMPLETED
            and self.review_completion_mode == "strict"
            and self.review_context is not None
            and self.final_review_report is not None
        ):
            # Older completed artifacts predate hunk ledgers; new finishes always write one.
            gaps = review_completion_gaps(
                self.review_context,
                self.final_review_report,
                allow_legacy_missing_hunk_ledger=(
                    self.final_review_report.coverage_ledger is None
                ),
            )
            if gaps:
                raise ValueError(
                    "strict completed session has incomplete review coverage: "
                    + "; ".join(gaps)
                )
        if (
            _review_artifact_payload_size(self.profile_reports, self.final_review_report)
            > _MAX_SESSION_REVIEW_ARTIFACT_BYTES
        ):
            raise ValueError("session review artifacts exceed the maximum serialized size")
        return self


class QaOrchestrationSummary(BaseModel):
    """Bounded metadata for finding a retained orchestration session."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(pattern=r"^qar-[0-9a-f]{32}$")
    task_type: QaTaskType
    status: OrchestrationStatus
    current_step: OrchestrationStep
    selected_bundle: ReviewBundle | None
    selected_profile: ReviewAgent | None
    current_profile: ReviewAgent | None
    review_repository: str | None = None
    review_head_sha: str | None = None
    review_diff_sha256: str | None = None
    has_review_artifacts: bool = False
    expires_at: AwareDatetime


class QaOrchestrationList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sessions: list[QaOrchestrationSummary]
    read_only: Literal[True] = True
    host_owns_decisions: Literal[True] = True


class QaOrchestrationDeletion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(pattern=r"^qar-[0-9a-f]{32}$")
    deleted: Literal[True] = Field(
        default=True,
        description="The run_id is absent from this server and its recovery store, including if already absent.",
    )


class AdvanceQaOrchestrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(pattern=r"^qar-[0-9a-f]{32}$")
    completed_step: OrchestrationStep
    status: QaTaskOutcome
    request_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    expected_revision: int | None = Field(default=None, ge=0, strict=True)
    selected_bundle: ReviewBundle | None = None
    selected_profile: ReviewAgent | None = None
    implementation_review: bool | None = None
    acknowledge_limited_profile: bool = False
    completed_profile: ReviewAgent | None = None
    completed_profiles: list[ReviewAgent] | None = None
    profile_reports: list[ProfileReviewReport] | None = Field(
        default=None, min_length=1, max_length=11
    )
    risk_signals: DeepReviewSignals | None = None
    skip_synthesis: bool = Field(
        default=False,
        description=(
            "Host attestation for a clean single-profile result with no findings, no unresolved "
            "material questions, and no risk signals. Defaults to the normal synthesis stage."
        ),
    )

    @model_validator(mode="after")
    def validate_transition_signal(self) -> "AdvanceQaOrchestrationRequest":
        if (self.implementation_review is not None or self.acknowledge_limited_profile) and not (
            self.completed_step is OrchestrationStep.TRIAGE and self.status == "completed"
        ):
            raise ValueError("implementation review intent is only valid for completed triage")
        if self.request_id is not None and self.expected_revision is None:
            raise ValueError("request_id requires expected_revision")
        if self.completed_profiles is not None and not self.completed_profiles:
            raise ValueError("completed_profiles must contain at least one profile")
        if self.completed_profile is not None and self.completed_profiles is not None:
            raise ValueError("completed_profile and completed_profiles are mutually exclusive")

        has_completed_profiles = (
            self.completed_profile is not None or self.completed_profiles is not None
        )
        if self.profile_reports is not None:
            if self.completed_step is not OrchestrationStep.PRIMARY_REVIEW or self.status != "completed":
                raise ValueError("profile_reports require a completed primary review")
            expected_profiles = (
                [self.completed_profile]
                if self.completed_profile is not None
                else self.completed_profiles
            )
            if [report.profile for report in self.profile_reports] != expected_profiles:
                raise ValueError("profile_reports must match completed profiles in order")
        if self.completed_step is OrchestrationStep.TRIAGE and self.status == "completed":
            if (self.selected_bundle is None) == (self.selected_profile is None):
                raise ValueError("exactly one selection is required after triage")
        elif self.selected_bundle is not None or self.selected_profile is not None:
            raise ValueError("selection may only be supplied after triage")

        if self.completed_step is OrchestrationStep.PRIMARY_REVIEW:
            if self.status == "completed" and not has_completed_profiles:
                raise ValueError(
                    "completed_profile or completed_profiles is required after primary review"
                )
            if self.status != "completed" and has_completed_profiles:
                raise ValueError(
                    "completed_profile or completed_profiles requires a completed primary review"
                )
        elif has_completed_profiles:
            raise ValueError(
                "completed_profile or completed_profiles may only be supplied after primary review"
            )

        if self.risk_signals is not None and (
            self.completed_step is not OrchestrationStep.PRIMARY_REVIEW
            or self.status != "completed"
        ):
            raise ValueError("risk signals must be sent with the final primary-review profile")
        if self.skip_synthesis and (
            self.completed_step is not OrchestrationStep.PRIMARY_REVIEW
            or self.status != "completed"
        ):
            raise ValueError("skip_synthesis may only follow a completed primary review")
        return self


class RenewQaOrchestrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(pattern=r"^qar-[0-9a-f]{32}$")
    expected_revision: int | None = Field(default=None, ge=0, strict=True)


class OrchestrationError(ValueError):
    """Raised when a host submits an invalid orchestration operation."""


def validate_final_report_profile_links(
    context: ReviewContext,
    profile_reports: Sequence[ProfileReviewReport],
    final_report: FinalReviewReport,
    *,
    allow_legacy: bool = False,
) -> None:
    """Validate synthesized candidate links against the fixed profile reports."""
    reported_candidates = {
        (report.profile, candidate.candidate_id): candidate
        for report in profile_reports
        for candidate in report.finding_candidates
    }
    if not allow_legacy and any(
        not finding.source_candidates for finding in final_report.finding_candidates
    ):
        raise OrchestrationError(
            "final findings require source candidates from profile reports"
        )

    source_candidates = {
        (source.profile, source.candidate_id)
        for finding in final_report.finding_candidates
        for source in finding.source_candidates
    }
    if not source_candidates.issubset(reported_candidates):
        raise OrchestrationError(
            "final finding source candidates do not match profile reports"
        )
    for finding in final_report.finding_candidates:
        if allow_legacy and not finding.source_candidates:
            continue
        source_findings = [
            reported_candidates[(source.profile, source.candidate_id)]
            for source in finding.source_candidates
        ]
        source_evidence_refs = {
            reference
            for source_finding in source_findings
            for reference in source_finding.evidence_refs
        }
        if not set(finding.evidence_refs).issubset(source_evidence_refs):
            raise OrchestrationError(
                "final finding cites evidence absent from its source candidates"
            )
        source_locations = {
            (location.path, location.start_line, location.end_line)
            for source_finding in source_findings
            for location in source_finding.source_locations
        }
        final_locations = {
            (location.path, location.start_line, location.end_line)
            for location in finding.source_locations
        }
        if not final_locations.issubset(source_locations):
            raise OrchestrationError(
                "final finding locations are absent from its source candidates"
            )
    if any(
        not set(finding.source_profiles).issubset(final_report.profiles_reviewed)
        for finding in final_report.finding_candidates
    ):
        raise OrchestrationError(
            "final finding source profiles do not match profiles_reviewed"
        )

    if allow_legacy:
        return
    decisions = {
        (decision.source.profile, decision.source.candidate_id): decision
        for decision in final_report.candidate_decisions
    }
    if set(decisions) != set(reported_candidates):
        raise OrchestrationError(
            "final report requires exactly one decision per submitted candidate"
        )
    for source, decision in decisions.items():
        candidate = reported_candidates[source]
        if decision.refutation is None:
            raise OrchestrationError("every candidate decision requires a refutation check")
        expected_refutation_status = {
            "confirmed": "not_refuted",
            "duplicate": "not_refuted",
            "rejected": "refuted",
            "unverified": "unverified",
        }[decision.verdict]
        if decision.refutation.status != expected_refutation_status:
            raise OrchestrationError(
                f"{decision.verdict} decisions require a {expected_refutation_status} check"
            )
        if decision.verdict in {"rejected", "unverified"} and (
            decision.candidate_snapshot != candidate
        ):
            raise OrchestrationError(
                "rejected and unverified decisions require the original candidate snapshot"
            )
        try:
            validate_refutation_check(context, candidate, decision.refutation)
        except ValueError as exc:
            raise OrchestrationError(str(exc)) from exc
    for finding in final_report.finding_candidates:
        linked = {
            source
            for source, decision in decisions.items()
            if decision.final_finding_id == finding.candidate_id
        }
        declared = {(source.profile, source.candidate_id) for source in finding.source_candidates}
        if linked != declared or not any(
            decisions[source].verdict == "confirmed" for source in linked
        ):
            raise OrchestrationError(
                "final finding sources must match confirmed and duplicate decisions"
            )
        if (
            finding.severity is None
            or not finding.source_locations
            or not finding.affected_area
            or not finding.affected_area.strip()
            or not finding.impact
            or not finding.impact.strip()
            or not finding.failure_condition
            or not finding.flip_condition
            or not finding.expected_behavior
            or not finding.actual_behavior
        ):
            raise OrchestrationError(
                "confirmed findings require severity, expected/actual behavior, "
                "failure/flip conditions, source locations, affected area, and impact"
            )


def validate_final_report_verification_records(
    context: ReviewContext,
    final_report: FinalReviewReport,
) -> None:
    """Require each verification receipt and its runtime evidence to match the pinned head."""
    registry = {entry.evidence_ref: entry for entry in context.evidence_registry}
    for verification in final_report.verification_records:
        if verification.head_sha != context.head_sha:
            raise OrchestrationError(
                "verification record does not match the bound head revision"
            )
        for reference in verification.evidence_refs:
            entry = registry.get(reference)
            if entry is not None and (
                entry.kind != "runtime" or entry.revision != context.head_sha
            ):
                raise OrchestrationError(
                    "verification evidence must be a runtime record bound to the reviewed head"
                )


_NEXT_ACTIONS = MappingProxyType(
    {
        OrchestrationStep.TRIAGE: "Host performs triage and submits the selected review bundle or profile.",
        OrchestrationStep.PRIMARY_REVIEW: "Host performs primary review with the selected ordered profiles.",
        OrchestrationStep.DEEP_REVIEW: "Host performs optional read-only deep analysis for the fixed escalation reason.",
        OrchestrationStep.SYNTHESIS: "Host performs final synthesis and validates the QA result.",
        OrchestrationStep.AWAITING_HOST_OUTCOME: "Host submits the final QA outcome with finish_qa_orchestration.",
    }
)
_TERMINAL_ACTIONS = MappingProxyType(
    {
        OrchestrationStatus.PARTIAL: "Host must finalize the partial QA outcome.",
        OrchestrationStatus.BLOCKED: "Host must finalize the blocked QA outcome.",
    }
)
_FINALIZED_TERMINAL_ACTIONS = MappingProxyType(
    {
        OrchestrationStatus.COMPLETED: "Host recorded the final QA outcome.",
        OrchestrationStatus.PARTIAL: "Host recorded the partial QA outcome.",
        OrchestrationStatus.BLOCKED: "Host recorded the blocked QA outcome.",
    }
)
_ACTIVE_SESSION_STATUSES = frozenset(
    {OrchestrationStatus.ACTIVE, OrchestrationStatus.AWAITING_HOST_OUTCOME}
)
_TERMINAL_SESSION_STATUSES = frozenset(
    {
        OrchestrationStatus.COMPLETED,
        OrchestrationStatus.PARTIAL,
        OrchestrationStatus.BLOCKED,
    }
)
_TASK_TYPE_ADAPTER = TypeAdapter(QaTaskType)


class QaOrchestrator:
    def __init__(
        self,
        *,
        ttl_seconds: int,
        max_sessions: int,
        model_selection: ModelPolicySelection | None = None,
        session_store: SqliteSessionStore | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_sessions <= 0:
            raise ValueError("max_sessions must be positive")
        self._ttl_seconds = ttl_seconds
        self._max_sessions = max_sessions
        self._model_selection = model_selection or DEFAULT_MODEL_SELECTION
        self._model_policies = build_model_policies(self._model_selection)
        self._clock = clock
        self._session_store = session_store
        self._sessions: dict[str, QaOrchestrationSession] = {}
        if session_store is not None:
            for session in session_store.load_all():
                has_review_artifacts = bool(session.profile_reports or session.final_review_report)
                if (
                    session.next_action in _FINALIZED_TERMINAL_ACTIONS.values()
                    and not has_review_artifacts
                ):
                    session_store.delete(session.run_id)
                    continue
                self._sessions[session.run_id] = session
        self._lock = RLock()
        if session_store is not None:
            now = self._clock()
            self._purge_expired(now)
            if self._active_session_count() > self._max_sessions:
                raise ValueError("session store exceeds the configured active session limit")
            self._evict_terminal_sessions_until_capacity(slots_required=0)

    @property
    def model_selection(self) -> ModelPolicySelection:
        return self._model_selection

    def start(
        self,
        task_type: QaTaskType,
        review_context: ReviewContext | None = None,
    ) -> QaOrchestrationSession:
        try:
            validated_task_type = _TASK_TYPE_ADAPTER.validate_python(task_type)
            validated_review_context = (
                ReviewContext.model_validate(review_context)
                if review_context is not None
                else None
            )
        except ValidationError as exc:
            raise OrchestrationError("invalid task type or review context") from exc

        with self._lock:
            now = self._clock()
            self._purge_expired(now)
            if self._active_session_count() >= self._max_sessions:
                raise OrchestrationError("session limit")
            self._evict_terminal_sessions_until_capacity()

            session = QaOrchestrationSession(
                run_id=f"qar-{uuid4().hex}",
                task_type=validated_task_type,
                status=OrchestrationStatus.ACTIVE,
                current_step=OrchestrationStep.TRIAGE,
                allowed_profiles=list(ReviewAgent),
                allowed_bundles=list(REVIEW_BUNDLES),
                recommended_bundles=list(
                    recommended_bundles_for(
                        validated_task_type,
                        validated_review_context.changed_files
                        if validated_review_context is not None
                        else (),
                    )
                ),
                recommended_profiles=list(
                    recommended_profiles_for_changed_files(
                        validated_review_context.changed_files
                        if validated_review_context is not None
                        else ()
                    )
                ),
                review_context=validated_review_context,
                review_completion_mode=(
                    "strict" if validated_review_context is not None else "legacy"
                ),
                model_policy=self._model_policies[OrchestrationStep.TRIAGE],
                next_action=_NEXT_ACTIONS[OrchestrationStep.TRIAGE],
                expires_at=now + timedelta(seconds=self._ttl_seconds),
            )
            start_sha256 = _stable_json_sha256(
                {
                    "task_type": str(validated_task_type),
                    "review_context": (
                        validated_review_context.model_dump(mode="json")
                        if validated_review_context is not None
                        else None
                    ),
                }
            )
            session = _append_audit_event(
                session,
                event_type="session_started",
                revision=session.revision,
                recorded_at=now,
                from_status=None,
                from_step=None,
                request_sha256=start_sha256,
            )
            session = session.model_copy(update={"audit_trace_complete": True})
            self._save_session(session)
            return self._copy(session)

    def advance(
        self,
        *,
        run_id: str,
        completed_step: OrchestrationStep,
        status: QaTaskOutcome,
        request_id: str | None = None,
        expected_revision: int | None = None,
        selected_bundle: ReviewBundle | None = None,
        selected_profile: ReviewAgent | None = None,
        implementation_review: bool | None = None,
        acknowledge_limited_profile: bool = False,
        completed_profile: ReviewAgent | None = None,
        completed_profiles: list[ReviewAgent] | None = None,
        profile_reports: list[ProfileReviewReport] | None = None,
        risk_signals: DeepReviewSignals | None = None,
        skip_synthesis: bool = False,
    ) -> QaOrchestrationSession:
        try:
            request = AdvanceQaOrchestrationRequest(
                run_id=run_id,
                completed_step=completed_step,
                status=status,
                request_id=request_id,
                expected_revision=expected_revision,
                selected_bundle=selected_bundle,
                selected_profile=selected_profile,
                implementation_review=implementation_review,
                acknowledge_limited_profile=acknowledge_limited_profile,
                completed_profile=completed_profile,
                completed_profiles=completed_profiles,
                profile_reports=profile_reports,
                risk_signals=risk_signals,
                skip_synthesis=skip_synthesis,
            )
        except ValidationError as exc:
            raise OrchestrationError("invalid transition signal") from exc

        with self._lock:
            now = self._clock()
            self._purge_expired(now, keep_run_id=request.run_id)
            session = self._require_session(request.run_id, now)
            excluded = set()
            if request.implementation_review is None:
                excluded.add("implementation_review")
            if not request.acknowledge_limited_profile:
                excluded.add("acknowledge_limited_profile")
            request_sha256 = hashlib.sha256(json.dumps(
                request.model_dump(mode="json", exclude=excluded), sort_keys=True, separators=(",", ":")
            ).encode("utf-8")).hexdigest()
            if request.request_id is not None:
                receipt = next((r for r in session.transition_receipts
                                if r.request_id == request.request_id), None)
                if receipt is not None:
                    if receipt.request_sha256 != request_sha256:
                        raise OrchestrationError("request_id reused with a different transition")
                    return self._copy(session)
                if len(session.transition_receipts) >= 128:
                    raise OrchestrationError("transition receipt limit")
            if request.expected_revision is not None and request.expected_revision != session.revision:
                raise OrchestrationError("stale session revision")
            if session.status is not OrchestrationStatus.ACTIVE:
                raise OrchestrationError("terminal session")
            if request.completed_step is not session.current_step:
                raise OrchestrationError("illegal transition")

            if request.status in ("partial", "blocked"):
                updated = self._refresh_expiry(
                    session.model_copy(
                        update={
                            "status": OrchestrationStatus(request.status),
                            "model_policy": None,
                            "next_action": _TERMINAL_ACTIONS[OrchestrationStatus(request.status)],
                        }
                    ),
                    now,
                )
                updated = self._record_transition(
                    session, updated, request, request_sha256, recorded_at=now
                )
                self._save_session(updated)
                return self._copy(updated)

            if (
                request.selected_profile is not None
                and session.selected_profile is not None
                and request.selected_profile != session.selected_profile
            ):
                raise OrchestrationError("changed profile after triage")

            updated = self._refresh_expiry(self._completed_transition(session, request), now)
            updated = self._record_transition(
                session, updated, request, request_sha256, recorded_at=now
            )
            self._save_session(updated)
            return self._copy(updated)

    @staticmethod
    def _record_transition(
        previous: QaOrchestrationSession, updated: QaOrchestrationSession,
        request: AdvanceQaOrchestrationRequest, request_sha256: str,
        *,
        recorded_at: datetime,
    ) -> QaOrchestrationSession:
        revision = previous.revision + 1
        receipts = list(previous.transition_receipts)
        if request.request_id is not None:
            receipts.append(QaTransitionReceipt(request_id=request.request_id,
                                               request_sha256=request_sha256,
                                               applied_revision=revision))
        updated = updated.model_copy(update={"revision": revision, "transition_receipts": receipts})
        return _append_audit_event(
            updated,
            event_type="transition_applied",
            revision=revision,
            recorded_at=recorded_at,
            from_status=previous.status,
            from_step=previous.current_step,
            request_id=request.request_id,
            request_sha256=request_sha256,
        )

    def renew(self, run_id: str, expected_revision: int | None = None) -> QaOrchestrationSession:
        """Refresh only an active lease; workflow revision and reports stay unchanged."""
        try:
            request = RenewQaOrchestrationRequest(run_id=run_id, expected_revision=expected_revision)
        except ValidationError as exc:
            raise OrchestrationError("invalid lease renewal") from exc
        with self._lock:
            now = self._clock()
            session = self._require_session(request.run_id, now)
            if session.status not in _ACTIVE_SESSION_STATUSES:
                raise OrchestrationError("only active sessions can be renewed")
            if request.expected_revision is not None and request.expected_revision != session.revision:
                raise OrchestrationError("stale session revision")
            updated = self._refresh_expiry(session, now)
            self._save_session(updated)
            return self._copy(updated)

    def get(self, run_id: str) -> QaOrchestrationSession:
        with self._lock:
            now = self._clock()
            self._purge_expired(now, keep_run_id=run_id)
            return self._copy(self._require_session(run_id, now))

    def list_sessions(self) -> QaOrchestrationList:
        """Read non-expired session metadata without changing memory or persistence."""
        with self._lock:
            now = self._clock()
            retained = sorted(
                (session for session in self._sessions.values() if session.expires_at > now),
                key=lambda session: (session.expires_at, session.run_id),
                reverse=True,
            )
            return QaOrchestrationList(
                sessions=[
                    QaOrchestrationSummary(
                        run_id=session.run_id,
                        task_type=session.task_type,
                        status=session.status,
                        current_step=session.current_step,
                        selected_bundle=session.selected_bundle,
                        selected_profile=session.selected_profile,
                        current_profile=session.current_profile,
                        review_repository=(
                            session.review_context.repository
                            if session.review_context is not None
                            else None
                        ),
                        review_head_sha=(
                            session.review_context.head_sha
                            if session.review_context is not None
                            else None
                        ),
                        review_diff_sha256=(
                            session.review_context.diff_sha256
                            if session.review_context is not None
                            else None
                        ),
                        has_review_artifacts=bool(
                            session.profile_reports or session.final_review_report
                        ),
                        expires_at=session.expires_at,
                    )
                    for session in retained
                ]
            )

    def delete(self, run_id: str) -> QaOrchestrationDeletion:
        result = QaOrchestrationDeletion(run_id=run_id)
        with self._lock:
            self._delete_session(run_id)
        return result

    def finish(
        self,
        *,
        run_id: str,
        outcome: QaTaskOutcome,
        final_review_report: FinalReviewReport | None = None,
        expected_revision: int | None = None,
    ) -> QaOrchestrationSession:
        """Record the host-owned outcome and, when supplied, its structured review report."""
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 0):
            raise OrchestrationError("invalid expected revision")
        try:
            final_status = OrchestrationStatus(outcome)
        except ValueError as exc:
            raise OrchestrationError("invalid final outcome") from exc
        try:
            if final_review_report is None:
                validated_report = None
            else:
                report_payload = (
                    final_review_report.model_dump()
                    if isinstance(final_review_report, FinalReviewReport)
                    else final_review_report
                )
                if isinstance(report_payload, Mapping):
                    report_payload = dict(report_payload)
                    report_payload.pop("coverage_ledger", None)
                validated_report = FinalReviewReport.model_validate(report_payload)
        except ValidationError as exc:
            raise OrchestrationError("invalid final review report") from exc

        if final_status not in {
            OrchestrationStatus.COMPLETED,
            OrchestrationStatus.PARTIAL,
            OrchestrationStatus.BLOCKED,
        }:
            raise OrchestrationError("invalid final outcome")

        with self._lock:
            now = self._clock()
            self._purge_expired(now, keep_run_id=run_id)
            session = self._require_session(run_id, now)

            is_idempotent_legacy_replay = (
                session.status in _TERMINAL_SESSION_STATUSES
                and session.status is final_status
                and validated_report is not None
                and validated_report == session.final_review_report
            )
            if validated_report is not None:
                if session.review_context is None:
                    raise OrchestrationError("final review report requires a bound review context")
                if validated_report.profiles_reviewed != tuple(session.completed_profiles):
                    raise OrchestrationError(
                        "final report profiles do not match completed profiles"
                    )
                expected_rules = (
                    session.review_context.review_rules.digest
                    if session.review_context.review_rules
                    else None
                )
                if validated_report.review_rules_sha256 != expected_rules:
                    raise OrchestrationError("final report rules digest mismatch")
            if validated_report is not None and not is_idempotent_legacy_replay:
                try:
                    validated_report = FinalReviewReport.model_validate(
                        {
                            **validated_report.model_dump(),
                            "coverage_ledger": _build_review_coverage_ledger(
                                session.review_context,
                                session.profile_reports,
                                session.completed_profiles,
                            ),
                        }
                    )
                except ValidationError as exc:
                    raise OrchestrationError("invalid host-generated coverage ledger") from exc
                is_idempotent_legacy_replay = (
                    is_idempotent_legacy_replay
                    or (
                        session.status in _TERMINAL_SESSION_STATUSES
                        and session.status is final_status
                        and validated_report == session.final_review_report
                    )
                )

            if validated_report is not None and not is_idempotent_legacy_replay:
                try:
                    validated_report = complete_criterion_coverage(
                        session.review_context,
                        validated_report,
                    )
                except ValueError as exc:
                    raise OrchestrationError("invalid requirement and MR-claim coverage") from exc
                is_idempotent_legacy_replay = (
                    is_idempotent_legacy_replay
                    or (
                        session.status in _TERMINAL_SESSION_STATUSES
                        and session.status is final_status
                        and validated_report == session.final_review_report
                    )
                )

            if validated_report is not None and not is_idempotent_legacy_replay:
                try:
                    validated_report = complete_scenario_coverage(
                        session.review_context,
                        session.profile_reports,
                        validated_report,
                    )
                except ValueError as exc:
                    raise OrchestrationError("invalid behavior scenario coverage") from exc
                is_idempotent_legacy_replay = (
                    is_idempotent_legacy_replay
                    or (
                        session.status in _TERMINAL_SESSION_STATUSES
                        and session.status is final_status
                        and validated_report == session.final_review_report
                    )
                )

            if (expected_revision is not None and expected_revision != session.revision
                and not is_idempotent_legacy_replay):
                raise OrchestrationError("stale session revision")
            if (
                validated_report is not None
                and any(
                    not finding.source_candidates
                    for finding in validated_report.finding_candidates
                )
                and not is_idempotent_legacy_replay
            ):
                raise OrchestrationError(
                    "final findings require source candidates from profile reports"
                )

            if validated_report is not None:
                if final_status is OrchestrationStatus.COMPLETED and not is_idempotent_legacy_replay:
                    coverage_ledger = validated_report.coverage_ledger
                    if coverage_ledger is None:
                        raise OrchestrationError("host-generated coverage ledger is required")
                    profiles_without_receipts = [
                        profile.profile.value
                        for profile in coverage_ledger.profiles
                        if not profile.executor_input_receipt_verified
                    ]
                    if profiles_without_receipts:
                        missing_profiles = ", ".join(profiles_without_receipts[:10])
                        if len(profiles_without_receipts) > 10:
                            missing_profiles += ", ..."
                        raise OrchestrationError(
                            "completed review requires a matching executor input receipt with "
                            "completed CLI stdin delivery for every profile; missing or "
                            f"unverified: {missing_profiles}"
                        )
                    review_profiles = [
                        profile
                        for profile in coverage_ledger.profiles
                        if profile.profile is not ReviewAgent.CODE_EXPLORER
                    ]
                    delivered_excerpt_indexes = {
                        index
                        for profile in review_profiles
                        for index in profile.source_excerpt_included_path_indexes
                    }
                    missing_excerpt_indexes = sorted(
                        set(coverage_ledger.head_source_excerpt_path_indexes)
                        - delivered_excerpt_indexes
                    )
                    if missing_excerpt_indexes:
                        examples = ", ".join(
                            str(index) for index in missing_excerpt_indexes[:10]
                        )
                        if len(missing_excerpt_indexes) > 10:
                            examples += ", ..."
                        raise OrchestrationError(
                            "completed review requires a non-explorer profile to receive a "
                            f"verified source excerpt for each text path; uncovered path indexes: {examples}"
                        )
                    covered_indexes = {
                        index
                        for profile in review_profiles
                        for index in profile.reviewer_attested_path_indexes
                    }
                    missing_indexes = sorted(
                        set(range(coverage_ledger.changed_path_count))
                        - covered_indexes
                    )
                    if missing_indexes:
                        examples = ", ".join(str(index) for index in missing_indexes[:10])
                        if len(missing_indexes) > 10:
                            examples += ", ..."
                        raise OrchestrationError(
                            "completed review requires a non-explorer profile to attest every "
                            f"changed path; uncovered path indexes: {examples}"
                        )
                context_refs = set(session.review_context.evidence_refs)
                validate_final_report_verification_records(
                    session.review_context,
                    validated_report,
                )
                report_refs = {
                    reference
                    for finding in validated_report.finding_candidates
                    for reference in finding.evidence_refs
                }
                report_refs.update(
                    reference
                    for note in (*validated_report.coverage_gaps, *validated_report.unverified)
                    for reference in note.evidence_refs
                )
                report_refs.update(
                    reference
                    for decision in validated_report.candidate_decisions
                    for reference in decision.evidence_refs
                )
                report_refs.update(
                    reference
                    for decision in validated_report.candidate_decisions
                    if decision.candidate_snapshot is not None
                    for reference in decision.candidate_snapshot.evidence_refs
                )
                report_refs.update(
                    reference
                    for decision in validated_report.candidate_decisions
                    if decision.refutation is not None
                    for reference in decision.refutation.evidence_refs
                )
                report_refs.update(
                    reference
                    for item in validated_report.criterion_coverage
                    for reference in (
                        item.criterion_ref,
                        *item.implementation_refs,
                        *item.test_refs,
                    )
                )
                report_refs.update(
                    reference
                    for item in validated_report.scenario_coverage
                    for reference in item.evidence_refs
                )
                report_refs.update(
                    reference
                    for item in validated_report.verification_records
                    for reference in item.evidence_refs
                )
                unknown_refs = report_refs - context_refs
                if unknown_refs:
                    raise OrchestrationError("final review report cites unknown evidence refs")
                try:
                    validate_registered_locations(session.review_context, validated_report.finding_candidates)
                except ValueError as exc:
                    raise OrchestrationError(str(exc)) from exc
                validate_final_report_profile_links(
                    session.review_context,
                    session.profile_reports,
                    validated_report,
                    allow_legacy=is_idempotent_legacy_replay,
                )

            finish_sha256 = _stable_json_sha256(
                {
                    "outcome": final_status.value,
                    "final_review_report": (
                        validated_report.model_dump(mode="json")
                        if validated_report is not None
                        else None
                    ),
                }
            )

            if session.status in _TERMINAL_SESSION_STATUSES:
                if session.status is not final_status:
                    raise OrchestrationError("conflicting final outcome")
                final_action = _FINALIZED_TERMINAL_ACTIONS[final_status]
                already_finalized = session.next_action == final_action
                if (
                    validated_report is not None
                    and validated_report != session.final_review_report
                    and (already_finalized or session.final_review_report is not None)
                ):
                    raise OrchestrationError("conflicting final review report")
                if already_finalized:
                    return self._copy(session)
                updated = session.model_copy(
                    update={
                        "next_action": final_action,
                        "final_review_report": validated_report,
                        "revision": session.revision + 1,
                    }
                )
                updated = self._refresh_expiry(updated, now)
                updated = _append_audit_event(
                    updated,
                    event_type="session_finalized",
                    revision=updated.revision,
                    recorded_at=now,
                    from_status=session.status,
                    from_step=session.current_step,
                    request_sha256=finish_sha256,
                )
                self._save_session(
                    updated,
                    persist=bool(updated.profile_reports or updated.final_review_report),
                )
                return self._copy(updated)

            if session.status is not OrchestrationStatus.AWAITING_HOST_OUTCOME:
                raise OrchestrationError("outcome is not ready")
            if (
                final_status is OrchestrationStatus.COMPLETED
                and session.review_context is not None
                and validated_report is None
            ):
                raise OrchestrationError(
                    "completed review-bound sessions require a final review report"
                )
            if validated_report is not None and session.review_context is None:
                raise OrchestrationError("final review report requires a bound review context")
            if validated_report is not None:
                try:
                    validate_impact_coverage_context(
                        validated_report,
                        session.review_context,
                    )
                except ValueError as exc:
                    raise OrchestrationError(str(exc)) from exc
            if (
                final_status is OrchestrationStatus.COMPLETED
                and session.review_completion_mode == "strict"
            ):
                if session.review_context is None or validated_report is None:
                    raise OrchestrationError(
                        "strict completed reviews require a bound context and final report"
                    )
                gaps = review_completion_gaps(session.review_context, validated_report)
                if gaps:
                    raise OrchestrationError(
                        "completed review requires complete input and impact coverage: "
                        + "; ".join(gaps)
                    )

            updated = self._refresh_expiry(
                session.model_copy(
                    update={
                        "status": final_status,
                        "revision": session.revision + 1,
                        "final_review_report": validated_report,
                        "model_policy": None,
                        "next_action": _FINALIZED_TERMINAL_ACTIONS[final_status],
                    }
                ),
                now,
            )
            updated = _append_audit_event(
                updated,
                event_type="session_finalized",
                revision=updated.revision,
                recorded_at=now,
                from_status=session.status,
                from_step=session.current_step,
                request_sha256=finish_sha256,
            )
            self._save_session(
                updated,
                persist=bool(updated.profile_reports or updated.final_review_report),
            )
            return self._copy(updated)

    def _completed_transition(
        self,
        session: QaOrchestrationSession,
        request: AdvanceQaOrchestrationRequest,
    ) -> QaOrchestrationSession:
        if session.current_step is OrchestrationStep.TRIAGE:
            if (
                request.implementation_review is True
                and request.selected_profile in {ReviewAgent.PR_TEST_ANALYZER, ReviewAgent.CODE_EXPLORER}
                and not request.acknowledge_limited_profile
            ):
                raise OrchestrationError(
                    "This profile cannot report implementation findings. Choose an implementation "
                    "reviewer or bundle, or explicitly acknowledge the limitation."
                )
            selected_profiles = triage_review_profiles(
                selected_bundle=request.selected_bundle,
                selected_profile=request.selected_profile,
                implementation_review=request.implementation_review,
            )
            if not selected_profiles:
                raise OrchestrationError("missing profile or bundle after triage")
            selected_profile = selected_profiles[0]
            return session.model_copy(
                update={
                    "current_step": OrchestrationStep.PRIMARY_REVIEW,
                    "selected_bundle": request.selected_bundle,
                    "selected_profile": selected_profile,
                    "profile_reports": [],
                    "final_review_report": None,
                    "review_profiles": list(selected_profiles),
                    "current_profile": selected_profile,
                    "completed_profiles": [],
                    "deep_assessment": None,
                    "deep_reason_code": None,
                    "model_policy": self._model_policies[OrchestrationStep.PRIMARY_REVIEW],
                    "next_action": _NEXT_ACTIONS[OrchestrationStep.PRIMARY_REVIEW],
                }
            )

        if session.current_step is OrchestrationStep.PRIMARY_REVIEW:
            if session.current_profile is None or not session.review_profiles:
                raise OrchestrationError("missing current profile")
            next_profile_index = len(session.completed_profiles)
            if next_profile_index >= len(session.review_profiles):
                raise OrchestrationError("all review profiles are already completed")
            expected_profile = session.review_profiles[next_profile_index]
            if session.current_profile is not expected_profile:
                raise OrchestrationError("current profile state is inconsistent")
            if request.completed_profiles is not None:
                if session.selected_bundle is not None:
                    groups = bundle_execution_groups(session.selected_bundle)
                elif session.selected_profile is not None:
                    groups = tuple((profile,) for profile in session.review_profiles)
                else:
                    raise OrchestrationError("missing selected profile for execution group")

                completed_count = len(session.completed_profiles)
                offset = 0
                expected_group = None
                for group in groups:
                    if offset <= completed_count < offset + len(group):
                        expected_group = group[completed_count - offset :]
                        break
                    offset += len(group)
                if expected_group is None or tuple(request.completed_profiles) != expected_group:
                    raise OrchestrationError("completed profiles do not match next execution group")
                transition_profiles = list(expected_group)
            else:
                if request.completed_profile is not expected_profile:
                    raise OrchestrationError("completed profile does not match current profile")
                transition_profiles = [expected_profile]

            is_last_profile = next_profile_index + len(transition_profiles) == len(
                session.review_profiles
            )
            if session.review_context is None:
                if request.profile_reports is not None:
                    raise OrchestrationError(
                        "profile reports require a bound review context"
                    )
                transition_reports: list[ProfileReviewReport] = []
            else:
                if request.profile_reports is None:
                    raise OrchestrationError(
                        "profile_reports are required for a review-bound primary review"
                    )
                if tuple(report.profile for report in request.profile_reports) != tuple(
                    transition_profiles
                ):
                    raise OrchestrationError(
                        "profile reports do not match the next completed profiles"
                    )
                context_refs = set(session.review_context.evidence_refs)
                for report in request.profile_reports:
                    expected_rules = (session.review_context.review_rules.digest
                                      if session.review_context.review_rules else None)
                    if report.review_rules_sha256 != expected_rules:
                        raise OrchestrationError("profile report rules digest mismatch")
                    if set(report.reviewed_changed_paths) - set(
                        session.review_context.changed_files
                    ):
                        raise OrchestrationError(
                            "profile report attests changed paths outside the bound context"
                        )
                    if any(
                        not candidate.failure_condition
                        or not candidate.flip_condition
                        or not candidate.expected_behavior
                        or not candidate.actual_behavior
                        for candidate in report.finding_candidates
                    ):
                        raise OrchestrationError(
                            "defect candidates require failure_condition and flip_condition; "
                            "expected_behavior and actual_behavior are also required"
                        )
                    report_refs = {
                        reference
                        for finding in report.finding_candidates
                        for reference in finding.evidence_refs
                    }
                    report_refs.update(
                        reference
                        for note in (*report.coverage_gaps, *report.unverified)
                        for reference in note.evidence_refs
                    )
                    report_refs.update(
                        reference
                        for item in (*report.scenario_coverage, *report.targeted_scenario_audit)
                        for reference in item.evidence_refs
                    )
                    report_refs.update(entry.evidence_ref for entry in report.evidence_map)
                    if report_refs - context_refs:
                        raise OrchestrationError(
                            "profile report cites unknown evidence refs"
                        )
                    try:
                        validate_registered_locations(session.review_context, report.finding_candidates)
                        validate_context_selection(session.review_context, report)
                    except ValueError as exc:
                        raise OrchestrationError(str(exc)) from exc
                transition_reports = request.profile_reports

            if request.skip_synthesis and (
                not is_last_profile
                or session.selected_bundle is not None
                or len(session.review_profiles) != 1
            ):
                raise OrchestrationError(
                    "synthesis can be skipped only for a clean single-profile review"
                )
            if request.skip_synthesis and any(
                report.finding_candidates
                or report.coverage_gaps
                or report.unverified
                or any(
                    item.disposition in {"candidate", "unverified"}
                    for item in (*report.scenario_coverage, *report.targeted_scenario_audit)
                )
                for report in transition_reports
            ):
                raise OrchestrationError(
                    "synthesis cannot be skipped with findings, unresolved scenarios, coverage gaps, or unverified items"
                )
            completed_profiles = [*session.completed_profiles, *transition_profiles]
            profile_reports = [*session.profile_reports, *transition_reports]
            if not is_last_profile:
                if request.risk_signals is not None:
                    raise OrchestrationError(
                        "risk signals must be sent with the final primary-review profile"
                    )
                next_profile = session.review_profiles[len(completed_profiles)]
                return session.model_copy(
                    update={
                        "current_profile": next_profile,
                        "completed_profiles": completed_profiles,
                        "profile_reports": profile_reports,
                        "model_policy": self._model_policies[OrchestrationStep.PRIMARY_REVIEW],
                        "next_action": _NEXT_ACTIONS[OrchestrationStep.PRIMARY_REVIEW],
                    }
                )

            if needs_targeted_omission_audit(profile_reports, session.review_profiles):
                if request.risk_signals is not None:
                    raise OrchestrationError(
                        "risk signals must be assessed after the targeted omission audit"
                    )
                if request.skip_synthesis:
                    raise OrchestrationError(
                        "synthesis cannot be skipped while unresolved scenarios require an omission audit"
                    )
                return session.model_copy(
                    update={
                        "current_step": OrchestrationStep.PRIMARY_REVIEW,
                        "review_profiles": [
                            *session.review_profiles,
                            ReviewAgent.OMISSION_AUDITOR,
                        ],
                        "current_profile": ReviewAgent.OMISSION_AUDITOR,
                        "completed_profiles": completed_profiles,
                        "profile_reports": profile_reports,
                        "risk_signals": None,
                        "deep_assessment": None,
                        "deep_reason_code": None,
                        "model_policy": self._model_policies[
                            OrchestrationStep.PRIMARY_REVIEW
                        ],
                        "next_action": (
                            "Run one targeted read-only omission_auditor pass over the unresolved "
                            "scenarios before final risk assessment."
                        ),
                    }
                )

            if request.risk_signals is None:
                raise OrchestrationError(
                    "risk_signals are required for the final primary-review profile"
                )
            if session.review_context is not None:
                unknown_signal_refs = set(request.risk_signals.evidence_refs) - set(
                    session.review_context.evidence_refs
                )
                if unknown_signal_refs:
                    raise OrchestrationError("risk signals cite unknown evidence refs")
            if request.skip_synthesis and any(request.risk_signals.model_dump().values()):
                raise OrchestrationError(
                    "synthesis can be skipped only for a clean single-profile review"
                )
            assessment = assess_deep_review(request.risk_signals)
            if assessment.should_escalate:
                return session.model_copy(
                    update={
                        "current_step": OrchestrationStep.DEEP_REVIEW,
                        "current_profile": None,
                        "completed_profiles": completed_profiles,
                        "profile_reports": profile_reports,
                        "risk_signals": request.risk_signals,
                        "deep_assessment": assessment,
                        "deep_reason_code": assessment.reason_codes[0],
                        "model_policy": self._model_policies[OrchestrationStep.DEEP_REVIEW],
                        "next_action": _NEXT_ACTIONS[OrchestrationStep.DEEP_REVIEW],
                    }
                )
            if request.skip_synthesis:
                return session.model_copy(
                    update={
                        "status": OrchestrationStatus.AWAITING_HOST_OUTCOME,
                        "current_step": OrchestrationStep.AWAITING_HOST_OUTCOME,
                        "current_profile": None,
                        "completed_profiles": completed_profiles,
                        "profile_reports": profile_reports,
                        "risk_signals": request.risk_signals,
                        "deep_assessment": assessment,
                        "deep_reason_code": None,
                        "model_policy": None,
                        "next_action": _NEXT_ACTIONS[
                            OrchestrationStep.AWAITING_HOST_OUTCOME
                        ],
                    }
                )
            return session.model_copy(
                update={
                    "current_step": OrchestrationStep.SYNTHESIS,
                    "current_profile": None,
                    "completed_profiles": completed_profiles,
                    "profile_reports": profile_reports,
                    "risk_signals": request.risk_signals,
                    "deep_assessment": assessment,
                    "deep_reason_code": None,
                    "model_policy": self._model_policies[OrchestrationStep.SYNTHESIS],
                    "next_action": _NEXT_ACTIONS[OrchestrationStep.SYNTHESIS],
                }
            )

        if session.current_step is OrchestrationStep.DEEP_REVIEW:
            return session.model_copy(
                update={
                    "current_step": OrchestrationStep.SYNTHESIS,
                    "model_policy": self._model_policies[OrchestrationStep.SYNTHESIS],
                    "next_action": _NEXT_ACTIONS[OrchestrationStep.SYNTHESIS],
                }
            )

        if session.current_step is OrchestrationStep.SYNTHESIS:
            return session.model_copy(
                update={
                    "status": OrchestrationStatus.AWAITING_HOST_OUTCOME,
                    "current_step": OrchestrationStep.AWAITING_HOST_OUTCOME,
                    "model_policy": None,
                    "next_action": _NEXT_ACTIONS[OrchestrationStep.AWAITING_HOST_OUTCOME],
                }
            )

        raise OrchestrationError("illegal transition")

    def _require_session(self, run_id: str, now: datetime) -> QaOrchestrationSession:
        session = self._sessions.get(run_id)
        if session is None:
            raise OrchestrationError("unknown run_id")
        if session.expires_at <= now:
            if self._session_store is not None:
                self._session_store.delete(run_id)
            raise OrchestrationError("expired session")
        return session

    def _purge_expired(self, now: datetime, *, keep_run_id: str | None = None) -> None:
        expired_run_ids = [
            run_id
            for run_id, session in self._sessions.items()
            if run_id != keep_run_id and session.expires_at <= now
        ]
        if self._session_store is not None:
            self._session_store.delete_many(expired_run_ids)
        for run_id in expired_run_ids:
            self._sessions.pop(run_id, None)

    def _active_session_count(self) -> int:
        return sum(
            session.status in _ACTIVE_SESSION_STATUSES for session in self._sessions.values()
        )

    def _evict_terminal_sessions_until_capacity(self, *, slots_required: int = 1) -> None:
        required_evictions = len(self._sessions) - self._max_sessions + slots_required
        if required_evictions <= 0:
            return
        terminal_run_ids = sorted(
            (
                (session.expires_at, run_id)
                for run_id, session in self._sessions.items()
                if session.status in _TERMINAL_SESSION_STATUSES
            ),
        )
        if len(terminal_run_ids) < required_evictions:
            raise OrchestrationError("session limit")
        for _, run_id in terminal_run_ids[:required_evictions]:
            self._delete_session(run_id)

    def _save_session(
        self,
        session: QaOrchestrationSession,
        *,
        persist: bool = True,
    ) -> None:
        if (
            _review_artifact_payload_size(session.profile_reports, session.final_review_report)
            > _MAX_SESSION_REVIEW_ARTIFACT_BYTES
        ):
            raise OrchestrationError("session review artifacts exceed the maximum serialized size")
        if self._session_store is not None and persist:
            self._session_store.save(session)
        elif self._session_store is not None:
            self._session_store.delete(session.run_id)
        self._sessions[session.run_id] = session

    def _delete_session(self, run_id: str) -> None:
        if self._session_store is not None:
            self._session_store.delete(run_id)
        self._sessions.pop(run_id, None)

    def _refresh_expiry(
        self,
        session: QaOrchestrationSession,
        now: datetime,
    ) -> QaOrchestrationSession:
        return session.model_copy(update={"expires_at": now + timedelta(seconds=self._ttl_seconds)})

    @staticmethod
    def _copy(session: QaOrchestrationSession) -> QaOrchestrationSession:
        return session.model_copy(deep=True)
