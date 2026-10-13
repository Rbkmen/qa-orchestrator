from enum import StrEnum
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

type QaTaskType = Literal[
    "ordinary_review",
    "widget_review",
    "epic_analysis",
    "requirements_analysis",
    "qa_planning",
    "autotest_implementation",
    "other",
]
type QaTaskOutcome = Literal["completed", "partial", "blocked"]


class ReviewAgent(StrEnum):
    PR_TEST_ANALYZER = "pr_test_analyzer"
    CODE_REVIEWER = "code_reviewer"
    SECURITY_REVIEWER = "security_reviewer"
    SILENT_FAILURE_HUNTER = "silent_failure_hunter"
    CODE_EXPLORER = "code_explorer"
    TYPESCRIPT_REVIEWER = "typescript_reviewer"
    REACT_REVIEWER = "react_reviewer"
    RUBY_REVIEWER = "ruby_reviewer"
    PYTHON_REVIEWER = "python_reviewer"
    MOBILE_REVIEWER = "mobile_reviewer"


class ReviewBundle(StrEnum):
    ORDINARY_MR = "ordinary_mr"
    WIDGET = "widget"
    WIDGET_JS = "widget_js"
    RUBY_BACKEND = "ruby_backend"
    PYTHON_BACKEND = "python_backend"
    MOBILE = "mobile"
    SECURITY = "security"
    AUTOTEST = "autotest"
    REQUIREMENTS = "requirements"


type GitObjectId = Annotated[str, Field(pattern=r"(?i)^(?:[a-f0-9]{40}|[a-f0-9]{64})$")]
type Sha256Digest = Annotated[str, Field(pattern=r"(?i)^[a-f0-9]{64}$")]
type EvidenceRefId = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$"),
]
type ChangedFilePath = Annotated[str, Field(min_length=1, max_length=1024)]

_MAX_PROFILE_REVIEW_REPORT_BYTES = 64 * 1024
_MAX_FINAL_REVIEW_REPORT_BYTES = 64 * 1024


class ReviewContext(BaseModel):
    """Immutable identifiers for the exact source and evidence packet reviewed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: str = Field(min_length=1, max_length=512)
    base_sha: GitObjectId
    head_sha: GitObjectId
    diff_sha256: Sha256Digest
    evidence_packet_sha256: Sha256Digest
    changed_files: tuple[ChangedFilePath, ...] = Field(min_length=1, max_length=500)
    evidence_refs: tuple[EvidenceRefId, ...] = Field(min_length=1, max_length=1000)

    @field_validator("repository")
    @classmethod
    def normalize_repository(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("repository must not be empty")
        if "?" in normalized or "#" in normalized:
            raise ValueError("repository identifier must not contain query or fragment data")
        if normalized.startswith("//"):
            raise ValueError("repository identifier must not contain clone credentials")
        if "://" in normalized:
            try:
                parsed = urlsplit(normalized)
            except ValueError as exc:
                raise ValueError(
                    "repository URL must not contain credentials or parameters"
                ) from exc
            if (
                parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("repository URL must not contain credentials or parameters")
        else:
            host, separator, _ = normalized.partition("/")
            if separator and "@" in host:
                raise ValueError("repository identifier must not contain clone credentials")
        return normalized

    @field_validator("base_sha", "head_sha", "diff_sha256", "evidence_packet_sha256")
    @classmethod
    def normalize_hashes(cls, value: str) -> str:
        return value.lower()

    @model_validator(mode="after")
    def validate_review_identity(self) -> "ReviewContext":
        if self.base_sha == self.head_sha:
            raise ValueError("base_sha and head_sha must identify different revisions")
        if any(not path.strip() for path in self.changed_files):
            raise ValueError("changed_files must contain non-empty paths")
        if len(set(self.changed_files)) != len(self.changed_files):
            raise ValueError("changed_files must be unique")
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("evidence_refs must be unique")
        return self


class ReviewFindingLocation(BaseModel):
    """A stable source locator for a finding candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: ChangedFilePath
    start_line: int = Field(ge=1)
    end_line: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_line_range(self) -> "ReviewFindingLocation":
        if self.end_line is not None and self.end_line < self.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        return self


class ProfileExecutionMetrics(BaseModel):
    """Optional host-reported timing and token counts for one completed profile."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    started_at: AwareDatetime
    completed_at: AwareDatetime
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_execution_interval(self) -> "ProfileExecutionMetrics":
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must be at or after started_at")
        return self


class ReviewFindingCandidate(BaseModel):
    """A possible defect with evidence references; the host still decides whether it is confirmed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str = Field(pattern=r"^F-[0-9]{2,}$")
    title: str = Field(min_length=1, max_length=256)
    description: str = Field(min_length=1, max_length=5000)
    confidence: Literal["high", "medium", "low"]
    verification_gap: str = Field(min_length=1, max_length=2000)
    evidence_refs: tuple[EvidenceRefId, ...] = Field(min_length=1, max_length=32)
    source_locations: tuple[ReviewFindingLocation, ...] = Field(
        default=(),
        max_length=20,
        description="Precise source locations supporting this candidate when available.",
    )
    affected_area: str | None = Field(
        default=None,
        max_length=256,
        description="Affected user flow or system area, when supported by the evidence.",
    )
    impact: str | None = Field(
        default=None,
        max_length=2000,
        description="Concrete effect on users or contracts; severity remains host-owned.",
    )

    @model_validator(mode="after")
    def validate_source_locations(self) -> "ReviewFindingCandidate":
        location_keys = {
            (location.path, location.start_line, location.end_line)
            for location in self.source_locations
        }
        if len(location_keys) != len(self.source_locations):
            raise ValueError("source_locations must be unique")
        return self


class ReviewFindingSource(BaseModel):
    """A profile-local candidate that contributed to one synthesized finding."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile: ReviewAgent
    candidate_id: str = Field(pattern=r"^F-[0-9]{2,}$")


class FinalReviewFinding(ReviewFindingCandidate):
    severity: Literal["high", "medium", "low"] | None = Field(
        default=None, description="Impact severity selected by the host for a confirmed finding."
    )
    source_profiles: tuple[ReviewAgent, ...] = Field(min_length=1)
    source_candidates: tuple[ReviewFindingSource, ...] = Field(
        default=(),
        description=(
            "Required for new final reports; empty only on reports persisted before source linkage."
        ),
    )

    @model_validator(mode="after")
    def validate_source_profiles(self) -> "FinalReviewFinding":
        if len(set(self.source_profiles)) != len(self.source_profiles):
            raise ValueError("source_profiles must be unique")
        if self.source_candidates:
            source_profiles = {source.profile for source in self.source_candidates}
            source_pairs = {
                (source.profile, source.candidate_id) for source in self.source_candidates
            }
            if len(source_pairs) != len(self.source_candidates):
                raise ValueError("source_candidates must be unique")
            if source_profiles != set(self.source_profiles):
                raise ValueError("source_profiles must match source_candidates")
        return self


class ReviewNote(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1, max_length=2000)
    evidence_refs: tuple[EvidenceRefId, ...] = Field(default=(), max_length=32)


class ReviewEvidenceMapEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_ref: EvidenceRefId
    description: str = Field(min_length=1, max_length=2000)


class ProfileReviewReport(BaseModel):
    """Structured host-reported result for one completed specialist profile."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile: ReviewAgent
    scope: str = Field(min_length=1, max_length=2000)
    finding_candidates: tuple[ReviewFindingCandidate, ...] = Field(default=(), max_length=100)
    coverage_gaps: tuple[ReviewNote, ...] = Field(default=(), max_length=100)
    unverified: tuple[ReviewNote, ...] = Field(default=(), max_length=100)
    evidence_map: tuple[ReviewEvidenceMapEntry, ...] = Field(default=(), max_length=500)
    execution_metrics: ProfileExecutionMetrics | None = Field(
        default=None,
        description="Optional host-reported execution data; the orchestrator does not verify it.",
    )

    @model_validator(mode="after")
    def validate_profile_scope(self) -> "ProfileReviewReport":
        if (
            self.profile in {ReviewAgent.CODE_EXPLORER, ReviewAgent.PR_TEST_ANALYZER}
            and self.finding_candidates
        ):
            raise ValueError(f"{self.profile.value} cannot report implementation findings")
        if len({entry.evidence_ref for entry in self.evidence_map}) != len(self.evidence_map):
            raise ValueError("evidence_map references must be unique")
        if len({finding.candidate_id for finding in self.finding_candidates}) != len(
            self.finding_candidates
        ):
            raise ValueError("finding candidate IDs must be unique within a profile report")
        if len(self.model_dump_json().encode("utf-8")) > _MAX_PROFILE_REVIEW_REPORT_BYTES:
            raise ValueError("profile report exceeds the maximum serialized size")
        return self


class ReviewCandidateDecision(BaseModel):
    """The host's disposition of one profile-local candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: ReviewFindingSource
    verdict: Literal["confirmed", "rejected", "duplicate", "unverified"]
    rationale: str = Field(min_length=1, max_length=2000)
    evidence_refs: tuple[EvidenceRefId, ...] = Field(min_length=1, max_length=32)
    final_finding_id: str | None = Field(default=None, pattern=r"^F-[0-9]{2,}$")

    @model_validator(mode="after")
    def validate_final_link(self) -> "ReviewCandidateDecision":
        if (self.verdict in {"confirmed", "duplicate"}) != (self.final_finding_id is not None):
            raise ValueError("only confirmed and duplicate decisions require final_finding_id")
        if not self.rationale.strip():
            raise ValueError("candidate decision rationale must not be blank")
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("candidate decision evidence_refs must be unique")
        return self


class FinalReviewReport(BaseModel):
    """Structured synthesis stored with a review-bound orchestration session."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str = Field(min_length=1, max_length=5000)
    profiles_reviewed: tuple[ReviewAgent, ...] = Field(min_length=1)
    finding_candidates: tuple[FinalReviewFinding, ...] = Field(default=(), max_length=100)
    coverage_gaps: tuple[ReviewNote, ...] = Field(default=(), max_length=100)
    unverified: tuple[ReviewNote, ...] = Field(default=(), max_length=100)
    candidate_decisions: tuple[ReviewCandidateDecision, ...] = Field(
        default=(), max_length=1000,
        description="One host decision per submitted candidate; absent on legacy saved reports."
    )

    @model_validator(mode="after")
    def validate_profiles_reviewed(self) -> "FinalReviewReport":
        if len(set(self.profiles_reviewed)) != len(self.profiles_reviewed):
            raise ValueError("profiles_reviewed must be unique")
        if len({finding.candidate_id for finding in self.finding_candidates}) != len(
            self.finding_candidates
        ):
            raise ValueError("finding candidate IDs must be unique within the final report")
        decision_sources = {
            (decision.source.profile, decision.source.candidate_id)
            for decision in self.candidate_decisions
        }
        if len(decision_sources) != len(self.candidate_decisions):
            raise ValueError("candidate decisions must have unique sources")
        finding_ids = {finding.candidate_id for finding in self.finding_candidates}
        if any(decision.final_finding_id is not None and decision.final_finding_id not in finding_ids
               for decision in self.candidate_decisions):
            raise ValueError("candidate decision links an unknown final finding")
        if len(self.model_dump_json().encode("utf-8")) > _MAX_FINAL_REVIEW_REPORT_BYTES:
            raise ValueError("final review report exceeds the maximum serialized size")
        return self


class ReviewRoute(BaseModel):
    profile: ReviewAgent
    display_name: str = Field(min_length=1)
    focus: str = Field(
        min_length=1,
        description="The profile's review boundary and primary responsibility.",
    )
    required_sections: list[str] = Field(
        min_length=1,
        description="Profile-specific output sections; use these instead of a generic template.",
    )
    constraints: list[str] = Field(
        min_length=1,
        description="Shared limits for this read-only review profile.",
    )
    escalation_signals: list[str] = Field(
        default_factory=list,
        description=(
            "Conditions to assess against evidence. A listed condition is not itself a deep-review trigger; "
            "set risk_signals only when the corresponding evidence-based condition applies."
        ),
    )
    read_only: Literal[True] = True
    host_owns_decisions: Literal[True] = True


class ReviewRouteSelection(BaseModel):
    routes: list[ReviewRoute] = Field(
        min_length=1,
        description=(
            "Detailed checklists in the selected route's declared profile order. "
            "Each route is a fixed, read-only checklist; it does not execute a review."
        )
    )
    execution_groups: list[list[ReviewAgent]] = Field(
        min_length=1,
        description=(
            "Ordered execution waves whose flattened profile IDs exactly match routes. Run the groups in order; "
            "profiles in the same group are independent and may be reviewed concurrently by the host. "
            "Wait for every profile in a group before reporting it with one completed_profiles call "
            "in the declared order. Older hosts may report each completed_profile separately."
        )
    )

    @model_validator(mode="after")
    def validate_execution_group_order(self) -> "ReviewRouteSelection":
        group_profiles = tuple(profile for group in self.execution_groups for profile in group)
        route_profiles = tuple(route.profile for route in self.routes)
        if (
            any(not group for group in self.execution_groups)
            or len(set(route_profiles)) != len(route_profiles)
            or group_profiles != route_profiles
        ):
            raise ValueError("execution_groups must match route profiles in fixed order")
        return self


class ReviewRouteBatch(ReviewRouteSelection):
    bundle: ReviewBundle = Field(description="Fixed bundle whose profile checklists are returned.")


class ReviewProfileSummary(BaseModel):
    profile: ReviewAgent
    display_name: str = Field(min_length=1)
    focus: str = Field(min_length=1, description="Review boundary for choosing this profile.")


class ReviewBundleSummary(BaseModel):
    bundle: ReviewBundle
    when_to_use: str = Field(min_length=1, description="Scope and stack suited to this bundle.")
    profiles: list[ReviewAgent] = Field(
        min_length=1, description="Ordered review profile identifiers; preserve this execution order."
    )
    execution_groups: list[list[ReviewAgent]] = Field(
        description=(
            "Ordered execution waves. Profiles in the same group are independent and may be reviewed "
            "concurrently; finish each group before starting the next."
        )
    )


class QaOrchestrationCatalog(BaseModel):
    profiles: list[ReviewProfileSummary]
    bundles: list[ReviewBundleSummary]
    recommended_bundles_by_task_type: dict[QaTaskType, list[ReviewBundle]] = Field(
        description="Task-based shortlists only; they do not restrict the available bundles."
    )
    read_only: Literal[True] = True
    host_owns_decisions: Literal[True] = True
