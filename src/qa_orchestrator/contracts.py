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
_MAX_FINAL_REVIEW_REPORT_BYTES = 80 * 1024


class EvidenceRecord(BaseModel):
    """Metadata for one evidence item; raw content remains with the host."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_ref: EvidenceRefId
    kind: Literal["source", "diff", "requirement", "claim", "runtime", "other"]
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


class ReviewInputSource(BaseModel):
    """Host-authored metadata about one declared source for a review."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: EvidenceRefId
    kind: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    locator: str = Field(min_length=1, max_length=1024)
    required: bool
    snapshot_sha256: Sha256Digest | None = None
    snapshot_revision: str | None = Field(default=None, min_length=1, max_length=128)
    retrieval_status: Literal["complete", "partial", "failed", "unknown"] = "unknown"
    pagination_status: Literal["complete", "incomplete", "not_applicable", "unknown"] = "unknown"
    extraction_status: Literal["complete", "partial", "failed", "unknown"] = "unknown"
    criterion_refs: tuple[EvidenceRefId, ...] = Field(default=(), max_length=500)

    @field_validator("locator")
    @classmethod
    def validate_stable_locator(cls, value: str) -> str:
        if value != value.strip() or any(character.isspace() for character in value):
            raise ValueError("source locator must be a stable single-line identifier")
        if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
            raise ValueError("source locator must not contain control characters")
        if "?" in value or "#" in value:
            raise ValueError("source locator must not contain query or fragment data")
        if value.startswith(("/", "\\", "//")) or "\\" in value:
            raise ValueError("source locator must be repository-relative or a stable URL")

        if "://" in value:
            try:
                parsed = urlsplit(value)
            except ValueError as exc:
                raise ValueError("source URL is malformed") from exc
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError("source URL must not contain credentials or unsupported schemes")
            path = parsed.path
        else:
            path = value

        if any(part in {".", ".."} for part in path.split("/")):
            raise ValueError("source locator must not contain relative path traversal")
        return value

    @field_validator("snapshot_sha256")
    @classmethod
    def normalize_snapshot_digest(cls, value: str | None) -> str | None:
        return value.lower() if value is not None else None

    @field_validator("snapshot_revision")
    @classmethod
    def validate_snapshot_revision(cls, value: str | None) -> str | None:
        if value is not None and (
            value != value.strip()
            or any(character.isspace() for character in value)
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        ):
            raise ValueError("snapshot revision must be a stable single-line identifier")
        return value

    @model_validator(mode="after")
    def validate_criterion_refs(self) -> "ReviewInputSource":
        if len(set(self.criterion_refs)) != len(self.criterion_refs):
            raise ValueError("source criterion refs must be unique")
        return self


class ReviewInputManifest(BaseModel):
    """Bounded metadata-only inventory of sources declared for a review."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sources: tuple[ReviewInputSource, ...] = Field(default=(), max_length=100)

    @model_validator(mode="after")
    def validate_unique_sources_and_criteria(self) -> "ReviewInputManifest":
        source_ids = [source.source_id for source in self.sources]
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("input manifest source IDs must be unique")

        criterion_refs = [
            criterion_ref
            for source in self.sources
            for criterion_ref in source.criterion_refs
        ]
        if len(set(criterion_refs)) != len(criterion_refs):
            raise ValueError("input manifest criterion refs must be unique")
        return self


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
    input_manifest: ReviewInputManifest | None = None
    neighbor_discovery_sha256: Sha256Digest | None = None

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

    @field_validator(
        "base_sha", "head_sha", "diff_sha256", "evidence_packet_sha256",
        "neighbor_discovery_sha256",
    )
    @classmethod
    def normalize_hashes(cls, value: str | None) -> str | None:
        return value.lower() if value is not None else None

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
            if self.input_manifest is not None:
                records_by_ref = {entry.evidence_ref: entry for entry in self.evidence_registry}
                for source in self.input_manifest.sources:
                    for criterion_ref in source.criterion_refs:
                        record = records_by_ref.get(criterion_ref)
                        if record is not None and record.kind not in {"requirement", "claim"}:
                            raise ValueError(
                                "input manifest criteria must resolve to requirement or claim evidence"
                            )
        return self

    @model_serializer(mode="wrap")
    def serialize_optional_input_manifest(self, handler):
        payload = handler(self)
        if self.input_manifest is None:
            payload.pop("input_manifest", None)
        if self.neighbor_discovery_sha256 is None:
            payload.pop("neighbor_discovery_sha256", None)
        return payload


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
    submitted_prompt_sha256: Sha256Digest | None = None
    prompt_write_status: Literal["prepared", "written"] | None = None
    source_packet_sha256: Sha256Digest | None = None
    selected_packet_sha256: Sha256Digest | None = None
    included_evidence_refs: tuple[EvidenceRefId, ...] = Field(default=(), max_length=1000)
    omitted_evidence_refs: tuple[EvidenceRefId, ...] = Field(default=(), max_length=1000)
    included_source_paths: tuple[ChangedFilePath, ...] = Field(default=(), max_length=500)
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
    review_head_sha: GitObjectId | None = None
    review_diff_sha256: Sha256Digest | None = None
    started_at: AwareDatetime
    completed_at: AwareDatetime
    queue_seconds: float = Field(ge=0, allow_inf_nan=False)
    executor_seconds: float = Field(ge=0, allow_inf_nan=False)

    @model_serializer(mode="wrap")
    def serialize_optional_input_receipt(self, handler):
        payload = handler(self)
        for field in ("review_head_sha", "review_diff_sha256", "submitted_prompt_sha256", "prompt_write_status", "source_packet_sha256", "selected_packet_sha256"):
            if getattr(self, field) is None:
                payload.pop(field, None)
        for field in ("included_evidence_refs", "omitted_evidence_refs", "included_source_paths"):
            if not getattr(self, field):
                payload.pop(field, None)
        return payload

    @model_validator(mode="after")
    def validate_interval(self) -> "ProfileAttemptMetrics":
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must be at or after started_at")
        if (
            self.cached_input_tokens is not None and self.input_tokens is not None
            and self.cached_input_tokens > self.input_tokens
        ):
            raise ValueError("cached input cannot exceed total input tokens")
        if self.prompt_write_status is not None and (
            self.submitted_prompt_sha256 is None or self.source_packet_sha256 is None
        ):
            raise ValueError("prompt delivery status requires prompt and source packet hashes")
        if self.submitted_prompt_sha256 is not None and self.prompt_write_status is None:
            raise ValueError("prompt hash requires a delivery status")
        if self.selected_packet_sha256 is None and (
            self.included_evidence_refs or self.omitted_evidence_refs or self.included_source_paths
        ):
            raise ValueError("packet selection metadata requires a selected packet hash")
        if len(set(self.included_evidence_refs)) != len(self.included_evidence_refs):
            raise ValueError("included evidence refs must be unique")
        if len(set(self.omitted_evidence_refs)) != len(self.omitted_evidence_refs):
            raise ValueError("omitted evidence refs must be unique")
        if set(self.included_evidence_refs) & set(self.omitted_evidence_refs):
            raise ValueError("included and omitted evidence refs must be disjoint")
        if len(set(self.included_source_paths)) != len(self.included_source_paths):
            raise ValueError("included source paths must be unique")
        if (self.review_head_sha is None) != (self.review_diff_sha256 is None):
            raise ValueError("review head and diff hashes must be supplied together")
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
        if self.attempts and self.attempt_count != len(self.attempts):
            raise ValueError("attempt_count must match the recorded attempt ledger")
        if len({attempt.attempt for attempt in self.attempts}) != len(self.attempts):
            raise ValueError("attempt numbers must be unique")
        if any(
            attempt.completed_at < self.started_at or attempt.started_at > self.completed_at
            for attempt in self.attempts
        ):
            raise ValueError("attempt interval must be within the profile execution interval")
        return self


class VerificationReceipt(BaseModel):
    """Provider-neutral metadata attesting that a run response was checked."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    verifier_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.-]*$")
    run_id: str = Field(
        min_length=1, max_length=256, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$"
    )
    head_sha: GitObjectId
    conclusion: Literal["passed", "failed", "skipped", "not_run", "cancelled", "unknown"]
    scope_sha256: Sha256Digest
    source_response_sha256: Sha256Digest
    verified_at: AwareDatetime

    @field_validator("run_id")
    @classmethod
    def validate_receipt_run_id(cls, value: str) -> str:
        if value != value.strip() or any(
            ord(character) < 0x20 or ord(character) == 0x7F for character in value
        ):
            raise ValueError("verification receipt run_id must be a stable single-line value")
        return value

    @field_validator("head_sha", "scope_sha256", "source_response_sha256")
    @classmethod
    def normalize_receipt_digests(cls, value: str) -> str:
        return value.lower()


class ReviewVerificationRecord(BaseModel):
    """Host-supplied test/CI/runtime result bound to the reviewed head revision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    verification_id: str = Field(pattern=r"^V-[0-9]{2,}$")
    check_type: Literal["test", "ci", "runtime"]
    scenario: str = Field(min_length=1, max_length=1000)
    status: Literal["passed", "failed", "skipped", "not_run", "cancelled", "unknown"]
    head_sha: GitObjectId
    environment: str = Field(min_length=1, max_length=256)
    source: Literal["host", "local_runner", "ci_provider", "runtime_system"]
    source_ref: str = Field(min_length=1, max_length=2048)
    run_url: str | None = Field(default=None, max_length=2048)
    reason: str | None = Field(default=None, max_length=2000)
    evidence_refs: tuple[EvidenceRefId, ...] = Field(default=(), max_length=100)
    started_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None
    scope: str | None = Field(default=None, min_length=1, max_length=2048)
    scope_sha256: Sha256Digest | None = None
    run_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$",
    )
    artifact_sha256: Sha256Digest | None = None
    provenance: Literal["host_reported", "local_reproduced", "provider_verified"] = "host_reported"
    receipt: VerificationReceipt | None = None

    @field_validator("scenario", "environment", "source_ref", "reason")
    @classmethod
    def nonblank_verification_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("verification record text must not be blank")
        return value.strip() if value is not None else None

    @field_validator("run_url")
    @classmethod
    def validate_run_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = urlsplit(value)
            hostname = parsed.hostname
        except ValueError as exc:
            raise ValueError("run_url must be an absolute HTTP(S) URL") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or any(char.isspace() or char in "<>" for char in value)
        ):
            raise ValueError("run_url must be an absolute HTTP(S) URL without credentials")
        return value

    @field_validator("scope", "run_id")
    @classmethod
    def validate_scope_and_run_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if (
            value != value.strip()
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        ):
            raise ValueError("verification scope and run_id must be stable single-line values")
        return value

    @field_validator("head_sha", "scope_sha256", "artifact_sha256")
    @classmethod
    def normalize_verification_digests(cls, value: str | None) -> str | None:
        return value.lower() if value is not None else None

    @model_validator(mode="after")
    def validate_result(self) -> "ReviewVerificationRecord":
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("verification evidence refs must be unique")
        if self.status in {"failed", "skipped", "not_run", "cancelled", "unknown"} and not self.reason:
            raise ValueError("non-passing verification records require a reason")
        if (self.started_at is None) != (self.completed_at is None):
            raise ValueError("verification start and completion times must be supplied together")
        if self.started_at is not None and self.completed_at < self.started_at:
            raise ValueError("verification completed_at must be at or after started_at")
        if self.scope_sha256 is not None and self.scope is None:
            raise ValueError("verification scope digest requires a scope")
        if self.scope is not None and self.scope_sha256 is not None:
            actual_scope_sha256 = hashlib.sha256(self.scope.encode("utf-8")).hexdigest()
            if self.scope_sha256 != actual_scope_sha256:
                raise ValueError("verification scope digest does not match scope")

        if self.provenance == "host_reported" and self.receipt is not None:
            raise ValueError("host-reported verification cannot carry a provider receipt")
        if self.provenance == "local_reproduced":
            if self.source != "local_runner":
                raise ValueError("local reproduction provenance requires local_runner source")
            if not all((self.scope, self.scope_sha256, self.run_id, self.artifact_sha256)):
                raise ValueError(
                    "local reproduction requires scope, scope digest, run_id, and artifact digest"
                )
            if self.started_at is None or self.completed_at is None:
                raise ValueError("local reproduction requires start and completion timestamps")
        if self.provenance == "provider_verified":
            receipt = self.receipt
            if self.source != "ci_provider":
                raise ValueError("provider verification provenance requires ci_provider source")
            if receipt is None:
                raise ValueError("provider verification provenance requires a verifier receipt")
            if not all((self.scope, self.scope_sha256, self.run_id)):
                raise ValueError("provider verification requires scope, scope digest, and run_id")
            if self.started_at is None or self.completed_at is None:
                raise ValueError("provider verification requires start and completion timestamps")
            if (
                receipt.run_id != self.run_id
                or receipt.head_sha != self.head_sha
                or receipt.conclusion != self.status
                or receipt.scope_sha256 != self.scope_sha256
            ):
                raise ValueError("provider verifier receipt does not match the verification record")
            if receipt.verified_at < self.completed_at:
                raise ValueError("provider verifier receipt verified_at precedes run completion")
        return self

    @model_serializer(mode="wrap")
    def serialize_additional_provenance(self, handler):
        payload = handler(self)
        for field_name in ("scope", "scope_sha256", "run_id", "artifact_sha256", "receipt"):
            if getattr(self, field_name) is None:
                payload.pop(field_name, None)
        if self.provenance == "host_reported":
            payload.pop("provenance", None)
        return payload


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
    expected_behavior: str | None = Field(
        default=None,
        min_length=1,
        max_length=2000,
        description=(
            "Expected result supported by a requirement, contract, or invariant; required for "
            "new submitted defect candidates."
        ),
    )
    actual_behavior: str | None = Field(
        default=None,
        min_length=1,
        max_length=2000,
        description=(
            "Source-derived result when the failure condition occurs; this is not runtime "
            "observation unless separately verified. Required for new submitted defect candidates."
        ),
    )

    @model_validator(mode="after")
    def validate_source_locations(self) -> "ReviewFindingCandidate":
        if (self.failure_condition is None) != (self.flip_condition is None):
            raise ValueError("provide both failure_condition and flip_condition")
        if (self.expected_behavior is None) != (self.actual_behavior is None):
            raise ValueError("provide both expected_behavior and actual_behavior")
        if self.failure_condition is not None and (
            not self.failure_condition.strip() or not self.flip_condition.strip()
        ):
            raise ValueError("refutation conditions must not be blank")
        if self.expected_behavior is not None and (
            not self.expected_behavior.strip() or not self.actual_behavior.strip()
        ):
            raise ValueError("expected and actual behavior must not be blank")
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
    """Host-recorded profile coverage and executor handoff status."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile: ReviewAgent
    context_selection_recorded: bool
    executor_input_receipt_verified: bool = False
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


class ImpactCandidateDisposition(BaseModel):
    """Host-authored decision for one candidate in a bounded impact scan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str = Field(min_length=1, max_length=256)
    relation: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    locator: ChangedFilePath
    disposition: Literal[
        "reviewed", "no_relevant_relationship", "unresolved", "not_checked"
    ]
    rationale: str = Field(min_length=1, max_length=2000)
    evidence_refs: tuple[EvidenceRefId, ...] = Field(default=(), max_length=32)

    @field_validator("candidate_id", "locator")
    @classmethod
    def validate_candidate_identifiers(cls, value: str) -> str:
        if value != value.strip() or any(
            ord(character) < 0x20 or ord(character) == 0x7F for character in value
        ):
            raise ValueError("impact candidate identifiers and locators must be stable single-line values")
        if not value:
            raise ValueError("impact candidate identifiers and locators must not be blank")
        return value

    @field_validator("locator")
    @classmethod
    def validate_repository_relative_locator(cls, value: str) -> str:
        if (
            value.startswith(("/", "\\"))
            or "\\" in value
            or ":" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
        ):
            raise ValueError("impact candidate locator must be repository-relative")
        return value

    @model_validator(mode="after")
    def validate_disposition(self) -> "ImpactCandidateDisposition":
        if not self.rationale.strip():
            raise ValueError("impact candidate rationale must not be blank")
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("impact candidate evidence refs must be unique")
        return self


class ImpactCoverageLimit(BaseModel):
    """One named numeric cap applied by the recorded bounded discovery run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    value: int = Field(strict=True, ge=0, le=2_147_483_647)
    reached: bool
    uninspected: bool

    @model_validator(mode="after")
    def validate_limit_state(self) -> "ImpactCoverageLimit":
        if self.uninspected and not self.reached:
            raise ValueError("an uninspected impact limit must have been reached")
        return self


class ImpactCoverageOmission(BaseModel):
    """Count and bounded examples for one class of discovery omissions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    count: int = Field(strict=True, ge=1, le=2_147_483_647)
    limit: int | None = Field(default=None, strict=True, ge=0, le=2_147_483_647)
    examples: tuple[str, ...] = Field(default=(), max_length=20)

    @field_validator("examples")
    @classmethod
    def validate_examples(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            not value.strip()
            or len(value) > 1024
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
            for value in values
        ):
            raise ValueError("impact omission examples must be bounded single-line values")
        return values


class ImpactCoverageLedger(BaseModel):
    """Disposition ledger scoped to one exact, bounded neighbor-discovery result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    head_sha: GitObjectId
    changed_paths_sha256: Sha256Digest
    discovery_sha256: Sha256Digest
    discovery_method: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    discovery_version: int = Field(strict=True, ge=1, le=2_147_483_647)
    discovery_state: Literal["ready", "partial", "unknown"]
    limits: tuple[ImpactCoverageLimit, ...] = Field(min_length=1, max_length=64)
    candidates: tuple[ImpactCandidateDisposition, ...] = Field(max_length=500)
    omissions: tuple[ImpactCoverageOmission, ...] = Field(max_length=100)

    @field_validator("head_sha", "changed_paths_sha256", "discovery_sha256")
    @classmethod
    def normalize_impact_digests(cls, value: str) -> str:
        return value.lower()

    @model_validator(mode="after")
    def validate_impact_coverage(self) -> "ImpactCoverageLedger":
        candidate_ids = [candidate.candidate_id for candidate in self.candidates]
        if len(set(candidate_ids)) != len(candidate_ids):
            raise ValueError("impact coverage candidate IDs must be unique")
        limit_names = [limit.name for limit in self.limits]
        if len(set(limit_names)) != len(limit_names):
            raise ValueError("impact coverage limit names must be unique")
        omission_kinds = [omission.kind for omission in self.omissions]
        if len(set(omission_kinds)) != len(omission_kinds):
            raise ValueError("impact coverage omission kinds must be unique")
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


class ReviewCriterionCoverage(BaseModel):
    """Host-authored trace from one requirement or MR claim to reviewed evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    criterion_ref: EvidenceRefId
    criterion_kind: Literal["requirement", "mr_claim"]
    expected_behavior: str | None = Field(default=None, max_length=2000)
    outcome: Literal["confirmed", "contradicted", "unverified", "not_applicable_to_diff"]
    implementation_refs: tuple[EvidenceRefId, ...] = Field(default=(), max_length=32)
    test_refs: tuple[EvidenceRefId, ...] = Field(default=(), max_length=32)
    rationale: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_coverage(self) -> "ReviewCriterionCoverage":
        if not self.rationale.strip():
            raise ValueError("criterion coverage rationale must not be blank")
        if self.expected_behavior is not None and not self.expected_behavior.strip():
            raise ValueError("expected_behavior must not be blank")
        if self.outcome != "unverified" and self.expected_behavior is None:
            raise ValueError("a verified criterion outcome requires expected_behavior")
        if self.outcome in {"confirmed", "contradicted"} and not self.implementation_refs:
            raise ValueError("a confirmed or contradicted criterion requires implementation evidence")
        for name, refs in (
            ("implementation_refs", self.implementation_refs),
            ("test_refs", self.test_refs),
        ):
            if len(set(refs)) != len(refs):
                raise ValueError(f"{name} must contain unique evidence refs")
        if set(self.implementation_refs) & set(self.test_refs):
            raise ValueError("implementation and test evidence refs must be distinct")
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
    criterion_coverage: tuple[ReviewCriterionCoverage, ...] = Field(default=(), max_length=100)
    verification_records: tuple[ReviewVerificationRecord, ...] = Field(
        default=(), max_length=250,
        description="Host-supplied test, CI and runtime checks bound to the reviewed head SHA.",
    )
    coverage_ledger: ReviewCoverageLedger | None = Field(
        default=None,
        description=(
            "Host-generated changed-path delivery facts plus executor inspection attestations. "
            "The host replaces this field when finalizing a bound review."
        ),
    )
    impact_coverage: ImpactCoverageLedger | None = Field(
        default=None,
        description=(
            "Host-authored dispositions for one bounded neighbor-discovery result; this does "
            "not claim exhaustive repository coverage."
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
        if len({item.criterion_ref for item in self.criterion_coverage}) != len(
            self.criterion_coverage
        ):
            raise ValueError("criterion coverage refs must be unique")
        if len({item.verification_id for item in self.verification_records}) != len(
            self.verification_records
        ):
            raise ValueError("verification IDs must be unique")
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

    @model_serializer(mode="wrap")
    def serialize_optional_impact_coverage(self, handler):
        payload = handler(self)
        if self.impact_coverage is None:
            payload.pop("impact_coverage", None)
        return payload


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
