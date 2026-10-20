import hashlib
import json
from enum import StrEnum
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

from qa_orchestrator.failure_diagnostics import sanitize_failure_detail
from qa_orchestrator.review_rules import ReviewRuleset

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
    OMISSION_AUDITOR = "omission_auditor"


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
type ChangedPathIndex = Annotated[int, Field(strict=True, ge=0, le=499)]


def review_changed_paths_sha256(paths: tuple[str, ...]) -> str:
    """Hash the ordered path list so coverage indexes cannot be detached from their diff."""
    canonical = json.dumps(list(paths), separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

# Added defaulted fields expand legacy persisted reports when they are loaded.
_MAX_PROFILE_REVIEW_REPORT_BYTES = 72 * 1024
_MAX_FINAL_REVIEW_REPORT_BYTES = 72 * 1024


class EvidenceRecord(BaseModel):
    """Metadata for one evidence item; raw content remains with the host."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_ref: EvidenceRefId
    kind: Literal["source", "diff", "requirement", "runtime", "other"]
    sha256: Sha256Digest
    revision: GitObjectId | None = None
    path: ChangedFilePath | None = None
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    locator: str | None = Field(default=None, min_length=1, max_length=1024)
    source_text_available: bool | None = None

    @field_validator("sha256", "revision")
    @classmethod
    def normalize_digests(cls, value: str | None) -> str | None:
        return value.lower() if value is not None else None

    @field_validator("path")
    @classmethod
    def repository_relative_path(cls, value: str | None) -> str | None:
        if value is not None and (
            value.startswith("/") or "\\" in value or ":" in value or "\x00" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
        ):
            raise ValueError("evidence source path must be repository-relative")
        return value

    @model_validator(mode="after")
    def validate_metadata(self) -> "EvidenceRecord":
        if self.kind == "source":
            if self.revision is None or self.path is None:
                raise ValueError("source evidence requires revision and path")
            if self.end_line is not None and (
                self.start_line is None or self.end_line < self.start_line
            ):
                raise ValueError("invalid evidence source line range")
        elif self.source_text_available is True:
            raise ValueError("source text availability applies only to source evidence")
        elif self.path is not None or self.start_line is not None or self.end_line is not None:
            raise ValueError("source coordinates are allowed only for source evidence")
        if self.kind == "diff" and self.revision is None:
            raise ValueError("diff evidence requires a head revision")
        if self.kind not in {"source", "diff"} and not self.locator:
            raise ValueError("external evidence requires a stable locator")
        return self

    @model_serializer(mode="wrap")
    def serialize_source_text_status(self, handler):
        payload = handler(self)
        if self.source_text_available is None:
            payload.pop("source_text_available", None)
        return payload


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
    review_rules: ReviewRuleset | None = None
    evidence_registry: tuple[EvidenceRecord, ...] = Field(
        default=(), max_length=1000,
        description="Optional complete ref-to-source registry with revision, coordinates and hashes."
    )

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
        if self.evidence_registry:
            ids = [entry.evidence_ref for entry in self.evidence_registry]
            if len(set(ids)) != len(ids) or set(ids) != set(self.evidence_refs):
                raise ValueError("evidence registry must map each registered ref exactly once")
            for entry in self.evidence_registry:
                if entry.kind == "source" and entry.revision not in {self.base_sha, self.head_sha}:
                    raise ValueError("source evidence revision must be the bound base or head")
                if entry.kind == "diff" and (
                    entry.revision != self.head_sha or entry.sha256 != self.diff_sha256
                ):
                    raise ValueError("diff evidence must match the bound head and diff hash")
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


class ProfileAttemptObservation(BaseModel):
    """Executor-supplied observations, including usage available before failure."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    preparation_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    model_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    validation_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    cli_failure_detail: str | None = Field(default=None, max_length=512)
    cli_exit_code: int | None = Field(default=None, ge=-255, le=255)
    cli_event_type: Literal["error", "turn.failed"] | None = None

    @field_validator("cli_failure_detail")
    @classmethod
    def sanitize_cli_failure_detail(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return sanitize_failure_detail(value) or None


class ProfileAttemptMetrics(ProfileAttemptObservation):
    """One call's timings, usage and sanitized CLI diagnostics; no raw exception/source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile: ReviewAgent
    attempt: int = Field(ge=1, le=4)
    outcome: Literal["completed", "retryable_error", "timeout", "failed", "cancelled"]
    started_at: AwareDatetime
    completed_at: AwareDatetime
    queue_seconds: float = Field(ge=0, allow_inf_nan=False)
    executor_seconds: float = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_interval(self) -> "ProfileAttemptMetrics":
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must be at or after started_at")
        if (
            self.cached_input_tokens is not None and self.input_tokens is not None
            and self.cached_input_tokens > self.input_tokens
        ):
            raise ValueError("cached input cannot exceed total input tokens")
        return self


class ProfileExecutionMetrics(BaseModel):
    """Optional host-reported timing and token counts for one completed profile."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    started_at: AwareDatetime
    completed_at: AwareDatetime
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    attempt_count: int = Field(default=1, ge=1, le=4)
    attempts: tuple[ProfileAttemptMetrics, ...] = Field(default=(), max_length=4)

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
    change_origin: Literal["introduced", "pre_existing", "unknown"] = Field(
        default="unknown",
        description="Whether the behavior is introduced by the reviewed change, pre-existing, or unknown.",
    )
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
    failure_condition: str | None = Field(
        default=None, min_length=1, max_length=2000,
        description=(
            "Specific preconditions that make the alleged failure occur; required for new "
            "submitted defect candidates. Optional only for legacy persisted payloads."
        ),
    )
    flip_condition: str | None = Field(
        default=None, min_length=1, max_length=2000,
        description=(
            "Concrete source/contract evidence that would refute this candidate; required for "
            "new submitted defect candidates. Optional only for legacy persisted payloads."
        ),
    )

    @model_validator(mode="after")
    def validate_source_locations(self) -> "ReviewFindingCandidate":
        if (self.failure_condition is None) != (self.flip_condition is None):
            raise ValueError("provide both failure_condition and flip_condition")
        if self.failure_condition is not None and (
            not self.failure_condition.strip() or not self.flip_condition.strip()
        ):
            raise ValueError("refutation conditions must not be blank")
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
    severity: Literal["blocker", "high", "medium", "low"] | None = Field(
        default=None, description="Impact severity selected by the host for a confirmed finding."
    )
    next_step: str | None = Field(
        default=None,
        min_length=1,
        max_length=2000,
        description="Targeted correction or verification step, when selected by the host.",
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


class ProfileContextSelection(BaseModel):
    """Host assertion of the exact subset actually sent to a profile."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    source_packet_sha256: Sha256Digest
    selected_packet_sha256: Sha256Digest
    included_refs: tuple[EvidenceRefId, ...] = Field(max_length=1000)
    omitted_refs: tuple[EvidenceRefId, ...] = Field(default=(), max_length=1000)
    budget_omitted_refs: tuple[EvidenceRefId, ...] = Field(default=(), max_length=1000)
    changed_files_count: int = Field(default=0, ge=0, le=10000)
    source_excerpt_files_available: int = Field(default=0, ge=0, le=10000)
    source_excerpt_files_included: int = Field(default=0, ge=0, le=10000)
    max_bytes: int = Field(ge=1, le=2_000_000)
    selected_bytes: int = Field(ge=0, le=2_000_000)

    @model_validator(mode="after")
    def validate_partition(self) -> "ProfileContextSelection":
        if (
            len(set(self.included_refs)) != len(self.included_refs)
            or len(set(self.omitted_refs)) != len(self.omitted_refs)
            or len(set(self.budget_omitted_refs)) != len(self.budget_omitted_refs)
            or set(self.included_refs) & set(self.omitted_refs)
            or not set(self.budget_omitted_refs).issubset(self.omitted_refs)
        ):
            raise ValueError("context selection refs must be a unique partition")
        if not (
            self.source_excerpt_files_included
            <= self.source_excerpt_files_available
            <= self.changed_files_count
        ):
            raise ValueError("source excerpt coverage counts are inconsistent")
        if self.selected_bytes > self.max_bytes:
            raise ValueError("context selection exceeds byte budget")
        return self


class ProfileChangedPathCoverage(BaseModel):
    """Host-recorded profile coverage plus optional executor-reported execution metrics."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile: ReviewAgent
    context_selection_recorded: bool
    source_excerpt_included_path_indexes: tuple[ChangedPathIndex, ...] = Field(
        default=(), max_length=500
    )
    reviewer_attested_path_indexes: tuple[ChangedPathIndex, ...] = Field(
        default=(), max_length=500
    )
    execution_metrics: ProfileExecutionMetrics | None = Field(
        default=None,
        description="Host-reported profile execution data; not independently verified by the orchestrator.",
    )

    @model_validator(mode="after")
    def validate_indexes(self) -> "ProfileChangedPathCoverage":
        for name, indexes in (
            ("source_excerpt_included_path_indexes", self.source_excerpt_included_path_indexes),
            ("reviewer_attested_path_indexes", self.reviewer_attested_path_indexes),
        ):
            if tuple(sorted(set(indexes))) != indexes:
                raise ValueError(f"{name} must be unique and sorted")
        if not self.context_selection_recorded and self.source_excerpt_included_path_indexes:
            raise ValueError("source excerpts cannot be included without a context selection")
        return self


class ReviewCoverageLedger(BaseModel):
    """Compact host-generated coverage facts bound to the ordered changed paths."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    changed_paths_sha256: Sha256Digest
    changed_path_count: int = Field(ge=1, le=500)
    registered_head_source_path_indexes: tuple[ChangedPathIndex, ...] = Field(
        default=(), max_length=500
    )
    head_source_excerpt_path_indexes: tuple[ChangedPathIndex, ...] = Field(
        default=(), max_length=500
    )
    head_source_text_unavailable_path_indexes: tuple[ChangedPathIndex, ...] = Field(
        default=(), max_length=500
    )
    profiles: tuple[ProfileChangedPathCoverage, ...] = Field(default=(), max_length=11)

    @field_validator("changed_paths_sha256")
    @classmethod
    def normalize_changed_paths_digest(cls, value: str) -> str:
        return value.lower()

    @model_validator(mode="after")
    def validate_coverage_indexes(self) -> "ReviewCoverageLedger":
        available = self.head_source_excerpt_path_indexes
        if tuple(sorted(set(available))) != available:
            raise ValueError("head source excerpt indexes must be unique and sorted")
        registered = self.registered_head_source_path_indexes
        unavailable = self.head_source_text_unavailable_path_indexes
        if tuple(sorted(set(registered))) != registered:
            raise ValueError("registered head source indexes must be unique and sorted")
        if tuple(sorted(set(unavailable))) != unavailable:
            raise ValueError("unavailable head source indexes must be unique and sorted")
        if any(
            index >= self.changed_path_count for index in (*registered, *available, *unavailable)
        ):
            raise ValueError("head source excerpt index is outside changed paths")
        if not set(available).issubset(registered) or not set(unavailable).issubset(registered):
            raise ValueError("head source text status requires a registered source record")
        if set(available) & set(unavailable):
            raise ValueError("head source text cannot be both available and unavailable")
        if len({profile.profile for profile in self.profiles}) != len(self.profiles):
            raise ValueError("coverage ledger profiles must be unique")
        for profile in self.profiles:
            indexes = (
                *profile.source_excerpt_included_path_indexes,
                *profile.reviewer_attested_path_indexes,
            )
            if any(index >= self.changed_path_count for index in indexes):
                raise ValueError("profile coverage index is outside changed paths")
            if not set(profile.source_excerpt_included_path_indexes).issubset(available):
                raise ValueError("included source excerpt indexes must have verified text")
        return self


class ProfileReviewReport(BaseModel):
    """Structured host-reported result for one completed specialist profile."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile: ReviewAgent
    context_selection: ProfileContextSelection | None = None
    review_rules_sha256: Sha256Digest | None = None
    scope: str = Field(min_length=1, max_length=2000)
    finding_candidates: tuple[ReviewFindingCandidate, ...] = Field(default=(), max_length=100)
    reviewed_changed_paths: tuple[ChangedFilePath, ...] = Field(
        default=(),
        max_length=500,
        description=(
            "Paths in the bound change that this profile explicitly inspected, including paths "
            "with no finding. This is an executor attestation; the host cannot independently "
            "verify that the inspection occurred."
        ),
    )
    coverage_gaps: tuple[ReviewNote, ...] = Field(default=(), max_length=100)
    unverified: tuple[ReviewNote, ...] = Field(default=(), max_length=100)
    evidence_map: tuple[ReviewEvidenceMapEntry, ...] = Field(default=(), max_length=500)
    execution_metrics: ProfileExecutionMetrics | None = Field(
        default=None,
        description="Optional host-reported execution data; the orchestrator does not verify it.",
    )

    @field_validator("review_rules_sha256")
    @classmethod
    def normalize_rules_digest(cls, value: str | None) -> str | None:
        return value.lower() if value is not None else None

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
        if len(set(self.reviewed_changed_paths)) != len(self.reviewed_changed_paths):
            raise ValueError("reviewed_changed_paths must be unique")
        if len(self.model_dump_json().encode("utf-8")) > _MAX_PROFILE_REVIEW_REPORT_BYTES:
            raise ValueError("profile report exceeds the maximum serialized size")
        return self


class RefutationCheck(BaseModel):
    """Host's evidence-backed assessment, not a vote or proof of another agent's execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    context_sha256: Sha256Digest
    candidate_sha256: Sha256Digest
    status: Literal["refuted", "not_refuted", "unverified"]
    rationale: str = Field(min_length=1, max_length=2000)
    evidence_refs: tuple[EvidenceRefId, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_rationale(self) -> "RefutationCheck":
        if not self.rationale.strip() or len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("refutation requires a nonblank rationale and unique refs")
        return self


class ReviewCandidateDecision(BaseModel):
    """The host's disposition of one profile-local candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: ReviewFindingSource
    verdict: Literal["confirmed", "rejected", "duplicate", "unverified"]
    rationale: str = Field(min_length=1, max_length=2000)
    evidence_refs: tuple[EvidenceRefId, ...] = Field(min_length=1, max_length=32)
    final_finding_id: str | None = Field(default=None, pattern=r"^F-[0-9]{2,}$")
    refutation: RefutationCheck | None = None
    candidate_snapshot: ReviewFindingCandidate | None = Field(
        default=None,
        description=(
            "Original profile candidate retained for rejected or unverified outcomes; new "
            "final reports must preserve it so possible bugs remain visible."
        ),
    )

    @model_validator(mode="after")
    def validate_final_link(self) -> "ReviewCandidateDecision":
        if (
            self.refutation is not None and self.verdict in {"confirmed", "duplicate"}
            and self.refutation.status != "not_refuted"
        ):
            raise ValueError("confirmation requires a resolved, not_refuted refutation check")
        if (self.verdict in {"confirmed", "duplicate"}) != (self.final_finding_id is not None):
            raise ValueError("only confirmed and duplicate decisions require final_finding_id")
        if (
            self.candidate_snapshot is not None
            and self.candidate_snapshot.candidate_id != self.source.candidate_id
        ):
            raise ValueError("candidate snapshot ID must match its decision source")
        if not self.rationale.strip():
            raise ValueError("candidate decision rationale must not be blank")
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("candidate decision evidence_refs must be unique")
        return self


class FinalReviewReport(BaseModel):
    """Structured synthesis stored with a review-bound orchestration session."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str = Field(min_length=1, max_length=5000)
    review_rules_sha256: Sha256Digest | None = None
    profiles_reviewed: tuple[ReviewAgent, ...] = Field(min_length=1)
    finding_candidates: tuple[FinalReviewFinding, ...] = Field(default=(), max_length=100)
    coverage_gaps: tuple[ReviewNote, ...] = Field(default=(), max_length=100)
    unverified: tuple[ReviewNote, ...] = Field(default=(), max_length=100)
    coverage_ledger: ReviewCoverageLedger | None = Field(
        default=None,
        description=(
            "Host-generated changed-path delivery facts plus executor inspection attestations. "
            "The host replaces this field when finalizing a bound review."
        ),
    )
    candidate_decisions: tuple[ReviewCandidateDecision, ...] = Field(
        default=(), max_length=1000,
        description="One host decision per submitted candidate; absent on legacy saved reports."
    )

    @field_validator("review_rules_sha256")
    @classmethod
    def normalize_rules_digest(cls, value: str | None) -> str | None:
        return value.lower() if value is not None else None

    @model_validator(mode="after")
    def validate_profiles_reviewed(self) -> "FinalReviewReport":
        if len(set(self.profiles_reviewed)) != len(self.profiles_reviewed):
            raise ValueError("profiles_reviewed must be unique")
        if (
            self.coverage_ledger is not None
            and tuple(profile.profile for profile in self.coverage_ledger.profiles)
            != self.profiles_reviewed
        ):
            raise ValueError("coverage ledger profiles must match profiles_reviewed")
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
