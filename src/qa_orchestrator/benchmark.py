"""Local benchmark scoring from pinned inputs and explicit external adjudications."""

import argparse
import hashlib
import json
import math
import re
import statistics
import subprocess
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping
from itertools import pairwise
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

from qa_orchestrator.contracts import (
    EvidenceRefId,
    GitObjectId,
    ProfileAttemptMetrics,
    ReviewAgent,
    ReviewFindingCandidate,
    Sha256Digest,
)
from qa_orchestrator.failure_diagnostics import sanitize_failure_detail


class GoldFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    gold_id: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=20000)
    severity: Literal["high", "medium", "low"]
    category: str = Field(min_length=1, max_length=128)
    path: str = Field(min_length=1, max_length=1024)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    scope: Literal["introduced-by-pr", "exacerbated-by-pr", "pre-existing"] | None = None
    difficulty: Literal["easy", "medium", "hard"] | None = None
    context_required: Literal[
        "diff-only", "diff-plus-related-files", "broader-project-context"
    ] | None = None

    @model_serializer(mode="wrap")
    def serialize_optional_reviewbench_metadata(self, handler):
        payload = handler(self)
        for field in ("scope", "difficulty", "context_required"):
            if getattr(self, field) is None:
                payload.pop(field, None)
        return payload

    @model_validator(mode="after")
    def valid_range(self) -> "GoldFinding":
        if self.end_line < self.start_line:
            raise ValueError("invalid golden line range")
        return self


def _normalize_pull_request_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            parsed.netloc.casefold(),
            parsed.path.rstrip("/").casefold(),
            "",
            "",
        )
    )


class BenchmarkCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(min_length=1, max_length=256)
    repository: str = Field(min_length=1, max_length=512)
    base_sha: GitObjectId
    head_sha: GitObjectId
    diff_sha256: Sha256Digest
    task_title: str | None = Field(default=None, max_length=4000)
    task_body: str | None = Field(default=None, max_length=30000)
    pull_request_url: str | None = Field(default=None, max_length=2048)
    split: Literal["tune", "check"] | None = None
    split_group_id: str | None = Field(default=None, min_length=1, max_length=512)
    language: str | None = Field(default=None, max_length=128)
    diff_bytes: int | None = Field(default=None, ge=0)
    task_context_sha256: Sha256Digest | None = None
    gold: tuple[GoldFinding, ...] = Field(max_length=1000)
    gold_complete: bool = False

    @model_serializer(mode="wrap")
    def serialize_optional_reviewbench_context(self, handler):
        payload = handler(self)
        for field in (
            "task_title",
            "task_body",
            "pull_request_url",
            "language",
            "diff_bytes",
            "task_context_sha256",
            "split",
            "split_group_id",
        ):
            if getattr(self, field) is None:
                payload.pop(field, None)
        return payload

    @field_validator("base_sha", "head_sha", "diff_sha256", "task_context_sha256")
    @classmethod
    def normalize_hashes(cls, value: str | None) -> str | None:
        return value.lower() if value is not None else None

    @field_validator("split_group_id")
    @classmethod
    def normalize_split_group_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("split_group_id must not be empty")
        return value

    @model_validator(mode="after")
    def unique_gold(self) -> "BenchmarkCase":
        if (self.task_title is not None or self.task_body is not None) != (
            self.task_context_sha256 is not None
        ):
            raise ValueError("task title/body and task context hash must be provided together")
        if self.task_context_sha256 is not None and self.task_context_sha256 != (
            _task_context_sha256(self.task_title, self.task_body)
        ):
            raise ValueError("task context hash does not match the pinned title/body")
        if self.base_sha == self.head_sha:
            raise ValueError("benchmark revisions must differ")
        if len({finding.gold_id for finding in self.gold}) != len(self.gold):
            raise ValueError("gold IDs must be unique within a case")
        if self.split is not None and self.split_group_id is None:
            raise ValueError("a split requires a split_group_id")
        return self


class BenchmarkDataset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1, 2, 3, 4] = 1
    kind: Literal["synthetic", "reviewbench_reference", "curated"]
    source: str = Field(min_length=1, max_length=1024)
    source_sha256: Sha256Digest | None = None
    source_revision_status: Literal["caller_supplied_unverified", "checkout_verified"] | None = None
    cases: tuple[BenchmarkCase, ...] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def unique_cases(self) -> "BenchmarkDataset":
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("case IDs must be unique")
        if self.schema_version == 1 and self.source_revision_status is not None:
            raise ValueError("schema version 1 cannot declare source revision status")
        if self.schema_version == 2 and self.source_revision_status != (
            "caller_supplied_unverified"
        ):
            raise ValueError("schema version 2 requires explicit source revision status")
        if self.schema_version == 3 and self.source_revision_status != "checkout_verified":
            raise ValueError("schema version 3 requires a verified source checkout")
        if self.schema_version == 4 and self.source_revision_status not in {
            "caller_supplied_unverified",
            "checkout_verified",
        }:
            raise ValueError("schema version 4 requires an explicit source revision status")
        if self.schema_version < 4 and any(case.split is not None for case in self.cases):
            raise ValueError("split assignments require dataset schema version 4")
        if self.schema_version == 4 and any(
            case.split is None or case.split_group_id is None for case in self.cases
        ):
            raise ValueError("schema version 4 requires a split and group ID for every case")
        if self.schema_version == 4 and {case.split for case in self.cases} != {
            "tune",
            "check",
        }:
            raise ValueError("schema version 4 requires cases in both tune and check splits")
        group_splits: dict[str, str] = {}
        pull_request_splits: dict[str, tuple[str | None, str | None]] = {}
        for case in self.cases:
            if case.split_group_id is not None and case.split is not None:
                previous_split = group_splits.setdefault(case.split_group_id, case.split)
                if previous_split != case.split:
                    raise ValueError("cases in the same split group must share one split")
            if case.pull_request_url is not None:
                pull_request_key = _normalize_pull_request_url(case.pull_request_url)
                if pull_request_key:
                    current_assignment = (case.split, case.split_group_id)
                    previous_assignment = pull_request_splits.setdefault(
                        pull_request_key, current_assignment
                    )
                    previous_split, previous_group_id = previous_assignment
                    current_split, current_group_id = current_assignment
                    if (
                        previous_split is not None
                        and current_split is not None
                        and previous_split != current_split
                    ) or (
                        previous_group_id is not None
                        and current_group_id is not None
                        and previous_group_id != current_group_id
                    ):
                        raise ValueError(
                            "cases for the same pull request must share one split and group ID"
                        )
                    pull_request_splits[pull_request_key] = (
                        previous_split if previous_split is not None else current_split,
                        previous_group_id if previous_group_id is not None else current_group_id,
                    )
        return self

    @model_serializer(mode="wrap")
    def serialize_revision_status(self, handler):
        payload = handler(self)
        if self.source_revision_status is None:
            payload.pop("source_revision_status", None)
        return payload


class BenchmarkQualityGatePolicy(BaseModel):
    """A named quality gate bound to one exact dataset identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_id: str = Field(min_length=1, max_length=128)
    dataset_kind: Literal["reviewbench_reference", "curated"]
    dataset_sha256: Sha256Digest
    min_cases: int = Field(ge=1, strict=True)
    min_gold_findings: int = Field(ge=1, strict=True)
    min_precision: float = Field(ge=0, le=1, strict=True)
    min_recall: float = Field(ge=0, le=1, strict=True)
    max_missed_high_impact: int = Field(ge=0, strict=True)
    min_evidence_validation_rate: float | None = Field(
        default=None, ge=0, le=1, strict=True
    )
    min_location_validation_rate: float | None = Field(
        default=None, ge=0, le=1, strict=True
    )
    min_stratum_recall: float | None = Field(default=None, ge=0, le=1, strict=True)
    min_stratum_gold_count: int = Field(default=5, ge=1, strict=True)
    min_speed_repetitions: int | None = Field(default=None, ge=2, le=20, strict=True)

    @field_validator("policy_id")
    @classmethod
    def normalize_policy_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("policy_id must not be empty")
        return value

    @field_validator("dataset_sha256")
    @classmethod
    def normalize_dataset_digest(cls, value: str) -> str:
        return value.lower()


class CandidateAdjudication(BaseModel):
    """A host/reviewer decision, never inferred from candidate wording or confidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str
    verdict: Literal["matched", "valid_unlisted", "false_positive", "duplicate", "unverified"]
    gold_id: str | None = None
    additional_gold_ids: tuple[str, ...] = Field(default=(), max_length=100)
    rationale: str = Field(min_length=1, max_length=5000)
    evidence_valid: bool | None = None
    location_valid: bool | None = None
    found_by: str | None = Field(default=None, min_length=1, max_length=256)
    judged_by: str | None = Field(default=None, min_length=1, max_length=256)

    @field_validator("found_by", "judged_by")
    @classmethod
    def normalize_provenance_actor(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("finding provenance actor must not be blank")
        return normalized

    @model_validator(mode="after")
    def match_requires_gold(self) -> "CandidateAdjudication":
        if (self.verdict in {"matched", "duplicate"}) != (self.gold_id is not None):
            raise ValueError("only matched and duplicate verdicts require gold_id")
        if self.additional_gold_ids and self.verdict != "matched":
            raise ValueError("additional gold matches require a matched verdict")
        all_gold = (self.gold_id, *self.additional_gold_ids)
        if len(set(all_gold)) != len(all_gold):
            raise ValueError("gold matches must be unique within an adjudication")
        return self

    @model_serializer(mode="wrap")
    def serialize_optional_provenance(self, handler):
        payload = handler(self)
        if self.found_by is None:
            payload.pop("found_by", None)
        if self.judged_by is None:
            payload.pop("judged_by", None)
        return payload


class MissedGoldDiagnosis(BaseModel):
    """Explicit post-run human analysis of why one complete-gold finding was missed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    gold_id: str = Field(min_length=1, max_length=128)
    cause: Literal[
        "evidence_packet_gap",
        "neighbor_trace_gap",
        "review_analysis_gap",
        "uncertain",
    ]
    rationale: str = Field(min_length=1, max_length=5000)
    evidence_refs: tuple[EvidenceRefId, ...] = Field(default=(), max_length=32)

    @field_validator("rationale")
    @classmethod
    def nonblank_rationale(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("missed gold diagnosis rationale must not be blank")
        return value

    @model_validator(mode="after")
    def unique_evidence_refs(self) -> "MissedGoldDiagnosis":
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("missed gold diagnosis evidence refs must be unique")
        return self


class BenchmarkRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1, max_length=128)
    case_id: str
    configuration_id: str = Field(min_length=1, max_length=256)
    model: str = Field(min_length=1, max_length=256)
    reasoning: str = Field(min_length=1, max_length=64)
    checklist_sha256: Sha256Digest
    host_instructions_sha256: Sha256Digest
    review_rules_sha256: Sha256Digest | None = None
    task_context_sha256: Sha256Digest | None = None
    base_sha: GitObjectId
    head_sha: GitObjectId
    diff_sha256: Sha256Digest
    evidence_packet_sha256: Sha256Digest
    elapsed_ms: int = Field(ge=0)
    queue_wait_ms: int | None = Field(default=None, ge=0)
    attempt: int | None = Field(default=None, ge=1, le=20)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    token_usage_scope: Literal["attempt_only", "includes_retries"] | None = Field(
        default=None,
        description="Whether run token counts exclude or include failed retry attempts.",
    )
    candidates: tuple[ReviewFindingCandidate, ...] = Field(default=(), max_length=1000)
    adjudicator: str = Field(min_length=1, max_length=256)
    adjudications: tuple[CandidateAdjudication, ...] = Field(default=(), max_length=1000)
    missed_gold_diagnoses: tuple[MissedGoldDiagnosis, ...] = Field(default=(), max_length=1000)

    @model_serializer(mode="wrap")
    def serialize_optional_task_context(self, handler):
        payload = handler(self)
        if self.task_context_sha256 is None:
            payload.pop("task_context_sha256", None)
        if self.queue_wait_ms is None:
            payload.pop("queue_wait_ms", None)
        if self.attempt is None:
            payload.pop("attempt", None)
        if self.token_usage_scope is None:
            payload.pop("token_usage_scope", None)
        if not self.missed_gold_diagnoses:
            payload.pop("missed_gold_diagnoses", None)
        return payload

    @field_validator(
        "base_sha",
        "head_sha",
        "diff_sha256",
        "evidence_packet_sha256",
        "checklist_sha256",
        "host_instructions_sha256",
        "review_rules_sha256",
        "task_context_sha256",
    )
    @classmethod
    def normalize_hashes(cls, value: str | None) -> str | None:
        return value.lower() if value is not None else None

    @model_validator(mode="after")
    def all_candidates_adjudicated(self) -> "BenchmarkRun":
        candidate_ids = [candidate.candidate_id for candidate in self.candidates]
        decision_ids = [decision.candidate_id for decision in self.adjudications]
        if len(set(candidate_ids)) != len(candidate_ids):
            raise ValueError("benchmark candidate IDs must be unique")
        if len(set(decision_ids)) != len(decision_ids) or set(candidate_ids) != set(decision_ids):
            raise ValueError("exactly one adjudication is required for every candidate")
        matched = [
            gold_id
            for d in self.adjudications
            if d.verdict == "matched"
            for gold_id in (d.gold_id, *d.additional_gold_ids)
        ]
        if len(set(matched)) != len(matched):
            raise ValueError("a gold finding can be matched only once per run")
        diagnosed_gold_ids = [item.gold_id for item in self.missed_gold_diagnoses]
        if len(set(diagnosed_gold_ids)) != len(diagnosed_gold_ids):
            raise ValueError("a missed gold finding can have only one diagnosis per run")
        return self


def benchmark_run_with_attempt_usage(
    run: BenchmarkRun,
    attempts: tuple[ProfileAttemptMetrics, ...],
    *,
    expected_profiles: tuple[ReviewAgent, ...],
) -> BenchmarkRun:
    """Bind a completed fresh profile ledger to one benchmark run without double counting."""
    if not expected_profiles or len(set(expected_profiles)) != len(expected_profiles):
        raise ValueError("expected profiles must be nonempty and unique")
    groups: dict[ReviewAgent, list[ProfileAttemptMetrics]] = defaultdict(list)
    for attempt in attempts:
        if attempt.profile not in expected_profiles:
            raise ValueError("attempt belongs to an unexpected profile")
        if (attempt.review_head_sha, attempt.review_diff_sha256) != (run.head_sha, run.diff_sha256):
            raise ValueError("attempt does not match the pinned benchmark input")
        if (attempt.source_packet_sha256 is not None
                and attempt.source_packet_sha256 != run.evidence_packet_sha256):
            raise ValueError("attempt does not match the benchmark evidence packet")
        groups[attempt.profile].append(attempt)
    if set(groups) != set(expected_profiles):
        raise ValueError("attempt ledger does not cover every expected profile")
    for values in groups.values():
        ordered = sorted(values, key=lambda item: item.attempt)
        if [item.attempt for item in ordered] != list(range(1, len(ordered) + 1)):
            raise ValueError("attempt ledger must be contiguous and unique")
        if ordered[-1].outcome != "completed" or any(
            item.outcome == "completed" for item in ordered[:-1]
        ):
            raise ValueError("each profile must have exactly one final completed attempt")
    def total(field: str) -> int | None:
        values = [getattr(attempt, field) for attempt in attempts]
        return sum(values) if all(
            attempt.source_packet_sha256 is not None for attempt in attempts
        ) and all(value is not None for value in values) else None
    return BenchmarkRun.model_validate({
        **run.model_dump(mode="python"),
        "input_tokens": total("input_tokens"),
        "output_tokens": total("output_tokens"),
        "token_usage_scope": "includes_retries",
    })


class BenchmarkFailure(BaseModel):
    """One failed benchmark attempt with bounded, sanitized execution metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(min_length=1, max_length=256)
    configuration_id: str = Field(min_length=1, max_length=256)
    model: str = Field(min_length=1, max_length=256)
    reasoning: str = Field(min_length=1, max_length=64)
    checklist_sha256: Sha256Digest
    host_instructions_sha256: Sha256Digest
    review_rules_sha256: Sha256Digest | None = None
    attempt: int = Field(ge=1, le=20)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    failure_code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    error_type: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")
    exit_code: int | None = Field(default=None, ge=-255, le=255)
    event_type: Literal["error", "turn.failed"] | None = None
    stderr_detail: str | None = Field(default=None, max_length=512)

    @field_validator(
        "checklist_sha256", "host_instructions_sha256", "review_rules_sha256"
    )
    @classmethod
    def normalize_hashes(cls, value: str | None) -> str | None:
        return value.lower() if value is not None else None

    @field_validator("stderr_detail")
    @classmethod
    def sanitize_stderr_detail(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return sanitize_failure_detail(value) or None

    @model_serializer(mode="wrap")
    def serialize_optional_diagnostics(self, handler):
        payload = handler(self)
        for field in (
            "review_rules_sha256",
            "exit_code",
            "event_type",
            "stderr_detail",
        ):
            if getattr(self, field) is None:
                payload.pop(field, None)
        return payload


class BenchmarkSchedule(BaseModel):
    """Host-declared successful run order for one benchmark execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schedule_id: str = Field(min_length=1, max_length=128)
    strategy: Literal["interleaved", "randomized"]
    run_order: tuple[str, ...] = Field(max_length=10000)

    @field_validator("run_order")
    @classmethod
    def validate_run_order(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not run_id.strip() or len(run_id) > 128 for run_id in value):
            raise ValueError("run_order IDs must be non-empty and at most 128 characters")
        if len(value) != len(set(value)):
            raise ValueError("run_order must contain unique run IDs")
        return value


class BenchmarkResults(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1, 2, 3, 4, 5] = 1
    runs: tuple[BenchmarkRun, ...] = Field(default=(), max_length=10000)
    failures: tuple[BenchmarkFailure, ...] = Field(default=(), max_length=10000)
    schedule: BenchmarkSchedule | None = None

    @model_validator(mode="after")
    def unique_runs(self) -> "BenchmarkResults":
        if not self.runs and not self.failures:
            raise ValueError("benchmark results must contain a run or a failure")
        if self.schema_version == 1 and (
            self.failures or any(run.attempt is not None for run in self.runs)
        ):
            raise ValueError("schema version 1 cannot contain attempt diagnostics")
        has_schedule_metadata = self.schedule is not None or any(
            run.queue_wait_ms is not None for run in self.runs
        )
        has_missed_gold_diagnoses = any(run.missed_gold_diagnoses for run in self.runs)
        if has_missed_gold_diagnoses and self.schema_version not in {4, 5}:
            raise ValueError("missed gold diagnoses require schema version 4 or 5")
        adjudications = [decision for run in self.runs for decision in run.adjudications]
        has_finding_provenance = any(
            decision.found_by is not None or decision.judged_by is not None
            for decision in adjudications
        )
        if has_finding_provenance and self.schema_version != 5:
            raise ValueError("per-finding provenance requires results schema version 5")
        if self.schema_version == 5 and any(
            decision.found_by is None or decision.judged_by is None
            for decision in adjudications
        ):
            raise ValueError(
                "schema version 5 requires both found_by and judged_by per adjudication"
            )
        if has_schedule_metadata and self.schema_version not in {3, 4, 5}:
            raise ValueError("schedule and queue metadata require schema version 3, 4 or 5")
        if self.schedule is not None:
            run_order = self.schedule.run_order
            run_ids = tuple(run.run_id for run in self.runs)
            if len(run_order) != len(run_ids) or set(run_order) != set(run_ids):
                raise ValueError("schedule run_order must list every successful run exactly once")
        if self.failures and any(run.attempt is None for run in self.runs):
            raise ValueError("failure artifacts require explicit attempt numbers for every run")
        if len({run.run_id for run in self.runs}) != len(self.runs):
            raise ValueError("run IDs must be unique")
        attempt_slots = {
            (run.configuration_id, run.case_id, run.attempt)
            for run in self.runs
            if run.attempt is not None
        }
        if len(attempt_slots) != sum(run.attempt is not None for run in self.runs):
            raise ValueError("benchmark attempt slots must be unique")
        failure_slots = [
            (failure.configuration_id, failure.case_id, failure.attempt)
            for failure in self.failures
        ]
        if len(set(failure_slots)) != len(failure_slots) or attempt_slots.intersection(
            failure_slots
        ):
            raise ValueError("benchmark attempt slots must be unique")
        configurations = {}
        for run in (*self.runs, *self.failures):
            identity = (
                run.model,
                run.reasoning,
                run.checklist_sha256,
                run.host_instructions_sha256,
                run.review_rules_sha256,
            )
            if configurations.setdefault(run.configuration_id, identity) != identity:
                raise ValueError(
                    "configuration_id cannot combine different model or prompt versions"
                )
        return self

    @model_serializer(mode="wrap")
    def serialize_optional_failures(self, handler):
        payload = handler(self)
        if not self.failures:
            payload.pop("failures", None)
        if self.schedule is None:
            payload.pop("schedule", None)
        return payload


def check_diff_budget(dataset: BenchmarkDataset, *, max_context_bytes: int) -> dict:
    """Identify raw diffs that cannot fit; a pass does not guarantee packet fit."""
    if type(max_context_bytes) is not int or max_context_bytes < 1:
        raise ValueError("max_context_bytes must be a positive integer")

    cases = []
    over_budget_case_ids = []
    unknown_case_ids = []
    for case in dataset.cases:
        if case.diff_bytes is None:
            status = "unknown"
            unknown_case_ids.append(case.case_id)
        elif case.diff_bytes > max_context_bytes:
            status = "raw_diff_over_budget"
            over_budget_case_ids.append(case.case_id)
        else:
            status = "raw_diff_within_budget"
        cases.append(
            {"case_id": case.case_id, "diff_bytes": case.diff_bytes, "status": status}
        )

    return {
        "schema_version": 1,
        "max_context_bytes": max_context_bytes,
        "case_count": len(cases),
        "over_budget_case_ids": over_budget_case_ids,
        "unknown_case_ids": unknown_case_ids,
        "scope": (
            "Checks raw diff bytes only. A raw diff over budget cannot fit; a diff within "
            "budget does not confirm that the serialized packet, source excerpts, or full "
            "executor prompt fit."
        ),
        "cases": cases,
    }


def _benchmark_dataset_sha256(dataset: BenchmarkDataset) -> str:
    """Fingerprint the validated dataset, including its snapshots and gold labels."""
    canonical = json.dumps(
        dataset.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def assign_benchmark_splits(
    dataset: BenchmarkDataset,
    *,
    check_fraction: float = 0.2,
    seed: str = "0",
) -> BenchmarkDataset:
    """Assign whole pull-request groups to stable tuning and check splits."""
    if type(check_fraction) not in {int, float} or not math.isfinite(check_fraction):
        raise ValueError("check_fraction must be a finite number between 0 and 1")
    if not 0 < check_fraction < 1:
        raise ValueError("check_fraction must be greater than 0 and less than 1")
    if not isinstance(seed, str) or not seed.strip():
        raise ValueError("seed must be a non-empty string")

    case_groups: dict[str, str] = {}
    groups: set[str] = set()
    explicit_ids_by_pull_request: dict[str, str] = {}
    for case in dataset.cases:
        if case.pull_request_url is None or case.split_group_id is None:
            continue
        normalized_url = _normalize_pull_request_url(case.pull_request_url)
        if not normalized_url:
            continue
        previous_group_id = explicit_ids_by_pull_request.setdefault(
            normalized_url, case.split_group_id
        )
        if previous_group_id != case.split_group_id:
            raise ValueError("cases for the same pull request must share one split group ID")

    for case in dataset.cases:
        normalized_url = (
            _normalize_pull_request_url(case.pull_request_url)
            if case.pull_request_url is not None
            else ""
        )
        if case.split_group_id is not None:
            group_id = case.split_group_id
        elif normalized_url and normalized_url in explicit_ids_by_pull_request:
            group_id = explicit_ids_by_pull_request[normalized_url]
        elif normalized_url:
            url_digest = hashlib.sha256(normalized_url.encode()).hexdigest()
            group_id = f"pr:{url_digest}"
        else:
            raise ValueError(
                "every case needs a pull_request_url or split_group_id before splitting"
            )
        case_groups[case.case_id] = group_id
        groups.add(group_id)

    if len(groups) < 2:
        raise ValueError("at least two distinct pull-request groups are required to split")

    check_count = max(1, min(len(groups) - 1, int(len(groups) * check_fraction + 0.5)))
    ordered_groups = sorted(
        groups,
        key=lambda group_id: hashlib.sha256(f"{seed}\0{group_id}".encode()).digest(),
    )
    check_groups = set(ordered_groups[:check_count])
    cases = []
    for case in dataset.cases:
        group_id = case_groups[case.case_id]
        cases.append(
            case.model_copy(
                update={
                    "split": "check" if group_id in check_groups else "tune",
                    "split_group_id": group_id,
                }
            ).model_dump(mode="json")
        )

    payload = dataset.model_dump(mode="json")
    payload.update(
        {
            "schema_version": 4,
            "source_revision_status": (
                dataset.source_revision_status or "caller_supplied_unverified"
            ),
            "cases": cases,
        }
    )
    return BenchmarkDataset.model_validate(payload)


def score_benchmark(
    dataset: BenchmarkDataset,
    results: BenchmarkResults,
    *,
    split: Literal["tune", "check"] | None = None,
) -> dict:
    if split is not None and split not in {"tune", "check"}:
        raise ValueError("split must be tune or check")
    if split is not None and dataset.schema_version != 4:
        raise ValueError("split scoring requires dataset schema version 4")
    all_cases = {case.case_id: case for case in dataset.cases}
    cases = {
        case_id: case
        for case_id, case in all_cases.items()
        if split is None or case.split == split
    }
    if split is not None and not cases:
        raise ValueError(f"dataset has no cases assigned to the {split} split")
    summaries = []
    failures_by_configuration = defaultdict(list)
    selected_failures = []
    for failure in results.failures:
        if failure.case_id not in all_cases:
            raise ValueError("benchmark failure references an unknown dataset case")
        if failure.case_id not in cases:
            continue
        selected_failures.append(failure)
        failures_by_configuration[failure.configuration_id].append(failure)
    for run in results.runs:
        case = all_cases.get(run.case_id)
        if case is None:
            raise ValueError("benchmark run references an unknown dataset case")
        if (case.base_sha, case.head_sha) != (run.base_sha, run.head_sha):
            raise ValueError("benchmark run does not match a pinned dataset case")
        if case.diff_sha256 != run.diff_sha256:
            raise ValueError("benchmark run diff does not match the pinned dataset case")
        if case.task_context_sha256 is not None and run.task_context_sha256 != (
            case.task_context_sha256
        ):
            raise ValueError("benchmark run task context does not match the pinned dataset case")
        if run.case_id not in cases:
            continue
        gold = {finding.gold_id: finding for finding in case.gold}
        if any(
            gold_id is not None and gold_id not in gold
            for d in run.adjudications
            for gold_id in (d.gold_id, *d.additional_gold_ids)
        ):
            raise ValueError("adjudication cites an unknown gold finding")
        matched = {
            gold_id
            for d in run.adjudications
            if d.verdict == "matched"
            for gold_id in (d.gold_id, *d.additional_gold_ids)
        }
        if any(d.verdict == "duplicate" and d.gold_id not in matched for d in run.adjudications):
            raise ValueError("duplicate verdict requires a matched original finding")
        missed_gold_diagnoses = {item.gold_id: item for item in run.missed_gold_diagnoses}
        if any(
            gold_id not in gold or gold_id in matched
            for gold_id in missed_gold_diagnoses
        ):
            raise ValueError(
                "missed gold diagnosis must reference a known unmatched gold finding"
            )
        if missed_gold_diagnoses and not case.gold_complete:
            raise ValueError("missed gold diagnosis requires a complete gold set")
        missed_gold = []
        for gold_id in sorted(set(gold) - matched):
            finding = gold[gold_id]
            diagnosis = missed_gold_diagnoses.get(gold_id)
            missed_gold.append(
                {
                    "gold_id": gold_id,
                    "severity": finding.severity,
                    "category": finding.category,
                    "path": finding.path,
                    "start_line": finding.start_line,
                    "end_line": finding.end_line,
                    "scope": finding.scope,
                    "difficulty": finding.difficulty,
                    "context_required": finding.context_required,
                    "diagnosis": diagnosis.cause if diagnosis else "unclassified",
                    "diagnosis_rationale": diagnosis.rationale if diagnosis else None,
                    "evidence_refs": list(diagnosis.evidence_refs) if diagnosis else [],
                }
            )
        valid = sum(d.verdict in {"matched", "valid_unlisted"} for d in run.adjudications)
        unresolved = sum(d.verdict == "unverified" for d in run.adjudications)
        decisions = {d.candidate_id: d for d in run.adjudications}
        confidence = {}
        for level in ("high", "medium", "low"):
            selected = [decisions[c.candidate_id] for c in run.candidates if c.confidence == level]
            level_valid = sum(d.verdict in {"matched", "valid_unlisted"} for d in selected)
            confidence[level] = {
                "count": len(selected),
                "valid": level_valid,
                "valid_rate": level_valid / len(selected) if selected else None,
                "unverified": sum(d.verdict == "unverified" for d in selected),
            }
        summaries.append(
            {
                "run_id": run.run_id,
                "case_id": run.case_id,
                "configuration_id": run.configuration_id,
                "evidence_packet_sha256": run.evidence_packet_sha256,
                "task_context_sha256": run.task_context_sha256,
                "candidates": len(run.candidates),
                "valid": valid,
                "matched_gold": len(matched),
                "gold_count": len(gold),
                "gold_complete": case.gold_complete,
                "missed_gold": missed_gold,
                "unverified": unresolved,
                "precision": valid / len(run.candidates) if run.candidates else None,
                "precision_is_lower_bound": bool(unresolved),
                "recall": len(matched) / len(gold) if case.gold_complete and gold else None,
                "missed_high_impact": sum(
                    f.severity == "high" and key not in matched for key, f in gold.items()
                ),
                "confidence": confidence,
                "evidence_validated": sum(d.evidence_valid is True for d in run.adjudications),
                "location_validated": sum(d.location_valid is True for d in run.adjudications),
                **(
                    {
                        "finding_provenance": [
                            {
                                "candidate_id": decision.candidate_id,
                                "found_by": decision.found_by,
                                "judged_by": decision.judged_by,
                            }
                            for decision in run.adjudications
                        ]
                    }
                    if results.schema_version == 5
                    else {}
                ),
                "elapsed_ms": run.elapsed_ms,
                "input_tokens": run.input_tokens,
                "output_tokens": run.output_tokens,
            }
        )
    by_configuration = defaultdict(list)
    runs_by_configuration = defaultdict(list)
    for summary in summaries:
        by_configuration[summary["configuration_id"]].append(summary)
    for run in results.runs:
        if run.case_id in cases:
            runs_by_configuration[run.configuration_id].append(run)
    aggregates = {}
    configurations = set(by_configuration) | set(failures_by_configuration)
    for configuration in sorted(configurations):
        rows = by_configuration.get(configuration, [])
        count = sum(r["candidates"] for r in rows)
        missing_cases = sorted(set(cases) - {r["case_id"] for r in rows})
        gold_attempt_count = sum(r["gold_count"] for r in rows)
        gold_count = sum(len(cases[case_id].gold) for case_id in {r["case_id"] for r in rows})
        selected_runs = runs_by_configuration[configuration]
        elapsed_values = sorted(row["elapsed_ms"] for row in rows)
        stratified_gold_recall = {}
        for dimension in (
            "severity",
            "category",
            "scope",
            "difficulty",
            "context_required",
        ):
            buckets = defaultdict(lambda: {"matched_gold": 0, "gold_count": 0})
            for run in selected_runs:
                case = cases[run.case_id]
                matched_ids = {
                    gold_id
                    for decision in run.adjudications
                    if decision.verdict == "matched"
                    for gold_id in (decision.gold_id, *decision.additional_gold_ids)
                }
                for finding in case.gold:
                    value = getattr(finding, dimension) or "unknown"
                    buckets[value]["gold_count"] += 1
                    buckets[value]["matched_gold"] += finding.gold_id in matched_ids
            stratified_gold_recall[dimension] = {
                value: {
                    **counts,
                    "recall": (
                        counts["matched_gold"] / counts["gold_count"]
                        if not missing_cases
                        and all(cases[run.case_id].gold_complete for run in selected_runs)
                        and counts["gold_count"]
                        else None
                    ),
                }
                for value, counts in sorted(buckets.items())
            }
        aggregates[configuration] = {
            "runs": len(rows),
            "cases": sorted({r["case_id"] for r in rows}),
            "case_count": len({r["case_id"] for r in rows}),
            "missing_cases": missing_cases,
            "coverage_complete": not missing_cases,
            "candidate_count": count,
            "gold_count": gold_count,
            "gold_attempt_count": gold_attempt_count,
            "unverified_candidates": sum(r["unverified"] for r in rows),
            "precision": sum(r["valid"] for r in rows) / count if count else None,
            "recall": sum(r["matched_gold"] for r in rows) / gold_attempt_count
            if not missing_cases
            and gold_attempt_count
            and all(r["recall"] is not None for r in rows)
            else None,
            "precision_is_lower_bound": any(r["unverified"] for r in rows),
            "missed_high_impact": sum(r["missed_high_impact"] for r in rows),
            "evidence_validation_rate": (
                sum(r["evidence_validated"] for r in rows) / count if count else None
            ),
            "location_validation_rate": (
                sum(r["location_validated"] for r in rows) / count if count else None
            ),
            "mean_elapsed_ms": sum(r["elapsed_ms"] for r in rows) / len(rows) if rows else None,
            "median_elapsed_ms": (
                statistics.median(r["elapsed_ms"] for r in rows) if rows else None
            ),
            "p95_elapsed_ms": (
                elapsed_values[math.ceil(0.95 * len(elapsed_values)) - 1]
                if elapsed_values
                else None
            ),
            "input_tokens": sum(r["input_tokens"] for r in rows)
            if rows and all(r["input_tokens"] is not None for r in rows)
            else None,
            "output_tokens": sum(r["output_tokens"] for r in rows)
            if rows and all(r["output_tokens"] is not None for r in rows)
            else None,
            "gold_recall_by": stratified_gold_recall,
        }
        failures = failures_by_configuration.get(configuration, [])
        # Failure records and successful runs may describe the same retry. Host execution
        # metrics include retries, so only combine them when every successful run explicitly
        # declares that its token counts cover that attempt alone.
        unambiguous_usage = not failures or all(
            run.token_usage_scope == "attempt_only" for run in selected_runs
        )
        for token_field in ("input_tokens", "output_tokens"):
            values = [r[token_field] for r in rows] + [getattr(f, token_field) for f in failures]
            aggregates[configuration][f"total_{token_field}_including_failures"] = (
                sum(values)
                if unambiguous_usage and values and all(value is not None for value in values)
                else None
            )
        if not unambiguous_usage:
            aggregates[configuration]["token_total_limitation"] = (
                "Successful run usage may include failed attempts; total cannot be determined."
            )
        if failures:
            aggregates[configuration]["failed_attempts"] = len(
                failures_by_configuration[configuration]
            )
    payload = {
        "schema_version": (
            5 if results.schema_version == 5 else 4 if selected_failures else 3
        ),
        "scoring": "local_explicit_adjudication",
        "dataset_kind": dataset.kind,
        "dataset_schema_version": dataset.schema_version,
        "dataset_split": split,
        "dataset_sha256": _benchmark_dataset_sha256(dataset),
        "dataset_case_count": len(all_cases),
        "dataset_cases": sorted(cases),
        "runs": summaries,
        "configurations": aggregates,
    }
    if selected_failures:
        payload["failures"] = [
            failure.model_dump(mode="json") for failure in selected_failures
        ]
    return payload


def _check_split_gate_reason(score: Mapping[str, object]) -> str | None:
    schema_version = score.get("dataset_schema_version")
    if (
        type(schema_version) is int
        and schema_version >= 4
        and score.get("dataset_split") != "check"
    ):
        return "split-aware dataset quality gates require scoring the check split"
    return None


def evaluate_quality_gate(
    score: dict,
    *,
    configuration_id: str,
    min_precision: float,
    min_recall: float,
    min_cases: int = 1,
    max_missed_high_impact: int = 0,
    min_evidence_validation_rate: float | None = None,
    min_location_validation_rate: float | None = None,
    max_mean_elapsed_ms: float | None = None,
    max_p95_elapsed_ms: float | None = None,
    min_stratum_recall: float | None = None,
    min_stratum_gold_count: int = 5,
    required_dataset_kind: str | None = None,
    required_dataset_sha256: str | None = None,
) -> dict:
    """Evaluate explicit quality and optional speed thresholds on a scored run."""
    if not isinstance(score, dict):
        raise TypeError("score must be a JSON object")
    if score.get("scoring") != "local_explicit_adjudication":
        raise ValueError("quality gate requires a local explicit-adjudication score")
    if not isinstance(configuration_id, str) or not configuration_id.strip():
        raise ValueError("configuration_id must not be empty")
    if type(min_cases) is not int or min_cases < 1:
        raise ValueError("min_cases must be a positive integer")
    if type(max_missed_high_impact) is not int or max_missed_high_impact < 0:
        raise ValueError("max_missed_high_impact must be a non-negative integer")
    if required_dataset_kind not in {None, "reviewbench_reference", "curated"}:
        raise ValueError("required_dataset_kind must be curated or reviewbench_reference")
    if required_dataset_sha256 is not None:
        if not isinstance(required_dataset_sha256, str) or len(required_dataset_sha256) != 64:
            raise ValueError("required_dataset_sha256 must be a 64-character SHA-256 digest")
        try:
            required_digest_bytes = bytes.fromhex(required_dataset_sha256)
        except ValueError as exc:
            raise ValueError(
                "required_dataset_sha256 must be a 64-character SHA-256 digest"
            ) from exc
        if required_digest_bytes.hex() != required_dataset_sha256.lower():
            raise ValueError("required_dataset_sha256 must be a 64-character SHA-256 digest")
        required_dataset_sha256 = required_digest_bytes.hex()

    def validate_rate(name: str, value: float | None, *, optional: bool) -> None:
        if value is None and optional:
            return
        if type(value) not in {int, float} or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError(f"{name} must be between 0 and 1")

    validate_rate("min_precision", min_precision, optional=False)
    validate_rate("min_recall", min_recall, optional=False)
    validate_rate(
        "min_evidence_validation_rate", min_evidence_validation_rate, optional=True
    )
    validate_rate(
        "min_location_validation_rate", min_location_validation_rate, optional=True
    )
    validate_rate("min_stratum_recall", min_stratum_recall, optional=True)
    if type(min_stratum_gold_count) is not int or min_stratum_gold_count < 1:
        raise ValueError("min_stratum_gold_count must be a positive integer")
    if max_mean_elapsed_ms is not None and (
        type(max_mean_elapsed_ms) not in {int, float}
        or not math.isfinite(max_mean_elapsed_ms)
        or max_mean_elapsed_ms < 0
    ):
        raise ValueError("max_mean_elapsed_ms must be a non-negative finite number")
    if max_p95_elapsed_ms is not None and (
        type(max_p95_elapsed_ms) not in {int, float}
        or not math.isfinite(max_p95_elapsed_ms)
        or max_p95_elapsed_ms < 0
    ):
        raise ValueError("max_p95_elapsed_ms must be a non-negative finite number")

    configurations = score.get("configurations")
    if not isinstance(configurations, dict) or configuration_id not in configurations:
        raise ValueError("quality gate configuration is absent from the score")
    aggregate = configurations[configuration_id]
    dataset_kind = score.get("dataset_kind")
    dataset_sha256 = score.get("dataset_sha256")
    if dataset_sha256 is not None:
        if not isinstance(dataset_sha256, str) or len(dataset_sha256) != 64:
            raise ValueError("score dataset_sha256 must be a 64-character SHA-256 digest")
        try:
            dataset_digest_bytes = bytes.fromhex(dataset_sha256)
        except ValueError as exc:
            raise ValueError("score dataset_sha256 must be a 64-character SHA-256 digest") from exc
        if dataset_digest_bytes.hex() != dataset_sha256.lower():
            raise ValueError("score dataset_sha256 must be a 64-character SHA-256 digest")
        dataset_sha256 = dataset_digest_bytes.hex()
    if not isinstance(aggregate, dict):
        raise TypeError("configuration aggregate must be an object")
    cases = aggregate.get("cases")
    missing_cases = aggregate.get("missing_cases")
    coverage_complete = aggregate.get("coverage_complete")
    if not isinstance(cases, list) or any(
        not isinstance(case, str) or not case.strip() for case in cases
    ):
        raise ValueError("configuration cases must be a list of non-empty strings")
    if not isinstance(missing_cases, list) or any(
        not isinstance(case, str) or not case.strip() for case in missing_cases
    ):
        raise ValueError("missing_cases must be a list of non-empty strings")
    if type(coverage_complete) is not bool or coverage_complete != (not missing_cases):
        raise ValueError("coverage_complete does not match missing_cases")

    def validate_observed_rate(name: str) -> float | int | None:
        value = aggregate.get(name)
        if value is not None and (
            type(value) not in {int, float}
            or not math.isfinite(value)
            or not 0 <= value <= 1
        ):
            raise ValueError(f"scored {name} must be between 0 and 1")
        return value

    precision = validate_observed_rate("precision")
    recall = validate_observed_rate("recall")
    evidence_validation_rate = validate_observed_rate("evidence_validation_rate")
    location_validation_rate = validate_observed_rate("location_validation_rate")
    precision_is_lower_bound = aggregate.get("precision_is_lower_bound")
    if type(precision_is_lower_bound) is not bool:
        raise ValueError("precision_is_lower_bound must be a boolean")
    missed_high_impact = aggregate.get("missed_high_impact")
    if type(missed_high_impact) is not int or missed_high_impact < 0:
        raise ValueError("missed_high_impact must be a non-negative integer")
    mean_elapsed_ms = aggregate.get("mean_elapsed_ms")
    if mean_elapsed_ms is not None and (
        type(mean_elapsed_ms) not in {int, float}
        or not math.isfinite(mean_elapsed_ms)
        or mean_elapsed_ms < 0
    ):
        raise ValueError("mean_elapsed_ms must be a non-negative finite number")
    p95_elapsed_ms = aggregate.get("p95_elapsed_ms")
    if p95_elapsed_ms is not None and (
        type(p95_elapsed_ms) not in {int, float}
        or not math.isfinite(p95_elapsed_ms)
        or p95_elapsed_ms < 0
    ):
        raise ValueError("p95_elapsed_ms must be a non-negative finite number")

    case_count = len(set(cases))
    observed = {
        "case_count": case_count,
        "cases": cases,
        "missing_cases": missing_cases,
        "coverage_complete": coverage_complete,
        "precision": precision,
        "precision_is_lower_bound": precision_is_lower_bound,
        "recall": recall,
        "missed_high_impact": missed_high_impact,
        "evidence_validation_rate": evidence_validation_rate,
        "location_validation_rate": location_validation_rate,
        "mean_elapsed_ms": mean_elapsed_ms,
        "p95_elapsed_ms": p95_elapsed_ms,
        "stratum_checks": [],
    }
    reasons = []
    split_reason = _check_split_gate_reason(score)
    if split_reason is not None:
        reasons.append(split_reason)
    if required_dataset_kind is not None and dataset_kind != required_dataset_kind:
        reasons.append(f"dataset kind {dataset_kind or 'unavailable'} does not match required kind")
    if required_dataset_sha256 is not None and dataset_sha256 != required_dataset_sha256:
        reasons.append("score dataset SHA-256 does not match the required pinned dataset")
    if not observed["coverage_complete"]:
        reasons.append("case coverage is incomplete")
    if case_count < min_cases:
        reasons.append(f"only {case_count} case(s) scored; {min_cases} required")
    if observed["precision"] is None:
        reasons.append("precision is unavailable")
    elif observed["precision"] < min_precision:
        reasons.append(f"precision {observed['precision']:.4f} is below {min_precision:.4f}")
    if observed["precision_is_lower_bound"]:
        reasons.append("precision is only a lower bound because adjudications are unresolved")
    if observed["recall"] is None:
        reasons.append("recall is unavailable")
    elif observed["recall"] < min_recall:
        reasons.append(f"recall {observed['recall']:.4f} is below {min_recall:.4f}")
    missed = observed["missed_high_impact"]
    if not isinstance(missed, int) or missed > max_missed_high_impact:
        count = missed if isinstance(missed, int) else "unknown"
        reasons.append(
            f"missed {count} high-impact gold finding(s); maximum is {max_missed_high_impact}"
        )
    for metric, threshold, label in (
        ("evidence_validation_rate", min_evidence_validation_rate, "evidence validation"),
        ("location_validation_rate", min_location_validation_rate, "location validation"),
    ):
        value = observed[metric]
        if threshold is not None and (value is None or value < threshold):
            reasons.append(
                f"{label} rate {value if value is not None else 'unavailable'} "
                f"is below {threshold:.4f}"
            )
    elapsed = observed["mean_elapsed_ms"]
    if max_mean_elapsed_ms is not None and (
        elapsed is None or elapsed > max_mean_elapsed_ms
    ):
        reasons.append(
            f"mean elapsed time {elapsed if elapsed is not None else 'unavailable'} ms "
            f"exceeds {max_mean_elapsed_ms:g} ms"
        )
    p95_elapsed = observed["p95_elapsed_ms"]
    if max_p95_elapsed_ms is not None and (
        p95_elapsed is None or p95_elapsed > max_p95_elapsed_ms
    ):
        reasons.append(
            f"p95 elapsed time {p95_elapsed if p95_elapsed is not None else 'unavailable'} ms "
            f"exceeds {max_p95_elapsed_ms:g} ms"
        )
    if min_stratum_recall is not None:
        stratified = aggregate.get("gold_recall_by")
        if not isinstance(stratified, dict):
            reasons.append("stratified gold recall is unavailable")
        else:
            if any(
                not isinstance(dimension, str) or not isinstance(buckets, dict)
                for dimension, buckets in stratified.items()
            ):
                raise TypeError("gold_recall_by must map dimensions to bucket objects")
            eligible = 0
            for dimension, buckets in sorted(stratified.items()):
                if any(
                    not isinstance(value, str) or not isinstance(metrics, dict)
                    for value, metrics in buckets.items()
                ):
                    raise TypeError("gold_recall_by buckets must be objects")
                for value, metrics in sorted(buckets.items()):
                    matched_count = metrics.get("matched_gold")
                    gold_count = metrics.get("gold_count")
                    recall_value = metrics.get("recall")
                    if (
                        type(matched_count) is not int
                        or type(gold_count) is not int
                        or matched_count < 0
                        or gold_count < matched_count
                    ):
                        raise ValueError("gold_recall_by counts are invalid")
                    if recall_value is not None and (
                        type(recall_value) not in {int, float}
                        or not math.isfinite(recall_value)
                        or not 0 <= recall_value <= 1
                    ):
                        raise ValueError("gold_recall_by recall must be between 0 and 1")
                    check = {
                        "dimension": dimension,
                        "value": value,
                        "matched_gold": matched_count,
                        "gold_count": gold_count,
                        "recall": recall_value,
                        "minimum_gold_count": min_stratum_gold_count,
                    }
                    if gold_count < min_stratum_gold_count:
                        check["status"] = "insufficient_sample"
                    else:
                        eligible += 1
                        check["status"] = "passed"
                        if recall_value is None:
                            check["status"] = "unavailable"
                            reasons.append(
                                f"{dimension}={value} recall is unavailable "
                                f"for {gold_count} gold finding(s)"
                            )
                        elif recall_value < min_stratum_recall:
                            check["status"] = "below_threshold"
                            reasons.append(
                                f"{dimension}={value} recall {recall_value:.4f} "
                                f"is below {min_stratum_recall:.4f} "
                                f"({matched_count}/{gold_count})"
                            )
                    observed["stratum_checks"].append(check)
            if eligible == 0:
                reasons.append(
                    "no gold-recall stratum meets the minimum sample count "
                    f"of {min_stratum_gold_count}"
                )
    return {
        "schema_version": 1,
        "mode": "experimental",
        "configuration_id": configuration_id,
        "dataset_kind": dataset_kind,
        "dataset_schema_version": score.get("dataset_schema_version"),
        "dataset_split": score.get("dataset_split"),
        "dataset_sha256": dataset_sha256,
        "passed": not reasons,
        "thresholds": {
            "min_cases": min_cases,
            "min_precision": min_precision,
            "min_recall": min_recall,
            "max_missed_high_impact": max_missed_high_impact,
            "min_evidence_validation_rate": min_evidence_validation_rate,
            "min_location_validation_rate": min_location_validation_rate,
            "max_mean_elapsed_ms": max_mean_elapsed_ms,
            "max_p95_elapsed_ms": max_p95_elapsed_ms,
            "min_stratum_recall": min_stratum_recall,
            "min_stratum_gold_count": min_stratum_gold_count,
            "required_dataset_kind": required_dataset_kind,
            "required_dataset_sha256": required_dataset_sha256,
        },
        "observed": observed,
        "reasons": reasons,
        "scope": (
            "Checks aggregate metrics from explicit host/reviewer adjudications. It cannot "
            "verify adjudicator correctness or prove that a model executed the review."
        ),
    }


def evaluate_quality_policy(
    score: Mapping[str, object],
    policy: BenchmarkQualityGatePolicy,
    *,
    configuration_id: str,
) -> dict:
    """Evaluate a named, digest-pinned quality policy with fail-closed data checks."""
    if not isinstance(policy, BenchmarkQualityGatePolicy):
        raise TypeError("policy must be a BenchmarkQualityGatePolicy")
    if not isinstance(score, Mapping):
        score = {}
        invalid_score = True
    else:
        score = dict(score)
        invalid_score = False

    dataset_kind = score.get("dataset_kind")
    dataset_sha256 = score.get("dataset_sha256")
    if isinstance(dataset_sha256, str):
        dataset_sha256 = dataset_sha256.lower()

    thresholds = {
        "dataset_kind": policy.dataset_kind,
        "dataset_sha256": policy.dataset_sha256,
        "min_cases": policy.min_cases,
        "min_gold_findings": policy.min_gold_findings,
        "min_precision": policy.min_precision,
        "min_recall": policy.min_recall,
        "max_missed_high_impact": policy.max_missed_high_impact,
        "min_evidence_validation_rate": policy.min_evidence_validation_rate,
        "min_location_validation_rate": policy.min_location_validation_rate,
        "min_stratum_recall": policy.min_stratum_recall,
        "min_stratum_gold_count": policy.min_stratum_gold_count,
        "min_speed_repetitions": policy.min_speed_repetitions,
    }
    observed = {
        "case_count": None,
        "gold_count": None,
        "failed_attempts": None,
        "precision": None,
        "precision_is_lower_bound": None,
        "recall": None,
        "missed_high_impact": None,
        "evidence_validation_rate": None,
        "location_validation_rate": None,
        "stratum_checks": [],
    }
    reasons: list[str] = []
    blocking: list[str] = []

    def block(reason: str) -> None:
        if reason not in reasons:
            reasons.append(reason)
        if reason not in blocking:
            blocking.append(reason)

    if invalid_score:
        block("score must be a JSON object")
    if score.get("scoring") != "local_explicit_adjudication":
        block("score is not from local explicit adjudication")
    split_reason = _check_split_gate_reason(score)
    if split_reason is not None:
        block(split_reason)
    if dataset_kind != policy.dataset_kind:
        block(
            f"dataset kind {dataset_kind or 'unavailable'} does not match policy kind "
            f"{policy.dataset_kind}"
        )
    valid_digest = False
    if isinstance(dataset_sha256, str) and len(dataset_sha256) == 64:
        try:
            valid_digest = bytes.fromhex(dataset_sha256).hex() == dataset_sha256
        except ValueError:
            valid_digest = False
    if not valid_digest:
        block("score dataset SHA-256 is missing or invalid")
    elif dataset_sha256 != policy.dataset_sha256:
        block("score dataset SHA-256 does not match the policy-pinned dataset")

    configurations = score.get("configurations")
    configuration_id_valid = (
        isinstance(configuration_id, str) and bool(configuration_id.strip())
    )
    if not configuration_id_valid:
        block("configuration_id is missing or invalid")
    aggregate = (
        configurations.get(configuration_id)
        if isinstance(configurations, Mapping) and configuration_id_valid
        else None
    )
    if not isinstance(aggregate, Mapping):
        block("configuration is absent or malformed in the score")
        aggregate = {}
    else:
        aggregate = dict(aggregate)

    raw_dataset_cases = score.get("dataset_cases")
    raw_cases = aggregate.get("cases")
    raw_missing_cases = aggregate.get("missing_cases")
    dataset_cases_valid = isinstance(raw_dataset_cases, list) and all(
        isinstance(case, str) and case.strip() for case in raw_dataset_cases
    )
    cases_valid = isinstance(raw_cases, list) and all(
        isinstance(case, str) and case.strip() for case in raw_cases
    )
    missing_cases_valid = isinstance(raw_missing_cases, list) and all(
        isinstance(case, str) and case.strip() for case in raw_missing_cases
    )
    if not (dataset_cases_valid and cases_valid and missing_cases_valid):
        block("dataset or configuration case coverage data is unavailable or malformed")
    else:
        dataset_case_set = set(raw_dataset_cases)
        case_set = set(raw_cases)
        missing_case_set = set(raw_missing_cases)
        if (
            len(dataset_case_set) != len(raw_dataset_cases)
            or len(case_set) != len(raw_cases)
            or len(missing_case_set) != len(raw_missing_cases)
            or case_set & missing_case_set
            or case_set | missing_case_set != dataset_case_set
            or missing_case_set != dataset_case_set - case_set
            or aggregate.get("coverage_complete") is not (not missing_case_set)
        ):
            block("case coverage is incomplete or inconsistent with the pinned dataset")
        observed["case_count"] = len(case_set)
        if len(case_set) < policy.min_cases:
            block(f"only {len(case_set)} case(s) scored; {policy.min_cases} required")

    gold_count = aggregate.get("gold_count")
    if type(gold_count) is not int or gold_count < 0:
        block("scored gold_count is unavailable or invalid")
    else:
        observed["gold_count"] = gold_count
        if gold_count < policy.min_gold_findings:
            block(
                f"only {gold_count} gold finding(s) covered; "
                f"{policy.min_gold_findings} required"
            )

    failed_attempts = aggregate.get("failed_attempts", 0)
    failures = score.get("failures", [])
    if type(failed_attempts) is not int or failed_attempts < 0:
        block("failed attempt count is unavailable or invalid")
    else:
        observed["failed_attempts"] = failed_attempts
        if failed_attempts:
            block(f"benchmark contains {failed_attempts} failed attempt(s)")
    if failures != []:
        block("benchmark contains failed attempt records")

    if type(aggregate.get("missed_high_impact")) is not int:
        block("missed high-impact finding count is unavailable or invalid")

    try:
        gate = evaluate_quality_gate(
            dict(score),
            configuration_id=configuration_id,
            min_cases=policy.min_cases,
            min_precision=policy.min_precision,
            min_recall=policy.min_recall,
            max_missed_high_impact=policy.max_missed_high_impact,
            min_evidence_validation_rate=policy.min_evidence_validation_rate,
            min_location_validation_rate=policy.min_location_validation_rate,
            min_stratum_recall=policy.min_stratum_recall,
            min_stratum_gold_count=policy.min_stratum_gold_count,
            required_dataset_kind=(
                policy.dataset_kind if policy.dataset_kind != "synthetic" else None
            ),
            required_dataset_sha256=policy.dataset_sha256,
        )
        observed.update(gate["observed"])
        observed["gold_count"] = gold_count if type(gold_count) is int else None
        observed["failed_attempts"] = (
            failed_attempts if type(failed_attempts) is int else None
        )
        for reason in gate["reasons"]:
            if reason not in reasons:
                reasons.append(reason)
            if any(
                marker in reason
                for marker in (
                    "unavailable",
                    "lower bound",
                    "incomplete",
                    "unknown",
                    "no gold-recall stratum meets",
                )
            ) and reason not in blocking:
                blocking.append(reason)
    except (TypeError, ValueError) as exc:
        block(f"scored metrics are malformed or unavailable: {exc}")
        gate = None

    state = "blocked" if blocking else "failed" if gate and not gate["passed"] else "passed"
    return {
        "schema_version": 1,
        "mode": "pinned_policy",
        "state": state,
        "policy_id": policy.policy_id,
        "configuration_id": configuration_id,
        "dataset_kind": dataset_kind,
        "dataset_schema_version": score.get("dataset_schema_version"),
        "dataset_split": score.get("dataset_split"),
        "dataset_sha256": dataset_sha256,
        "thresholds": thresholds,
        "observed": observed,
        "reasons": reasons,
        "scope": (
            "Checks score coverage and aggregate metrics from explicit host/reviewer "
            "adjudications. It cannot independently verify adjudicator correctness."
        ),
    }


def score_reliability(
    dataset: BenchmarkDataset, results: BenchmarkResults, *, expected_repetitions: int
) -> dict:
    if not 2 <= expected_repetitions <= 20:
        raise ValueError("repetitions must be between 2 and 20")
    scored = score_benchmark(dataset, results)
    summaries = {r["run_id"]: r for r in scored["runs"]}
    cases = {c.case_id: c for c in dataset.cases}
    grouped = defaultdict(list)
    for run in results.runs:
        grouped[(run.configuration_id, run.case_id)].append(run)
    failures_by_group = defaultdict(list)
    for failure in results.failures:
        if failure.attempt > expected_repetitions:
            raise ValueError("more attempts than the declared repetition budget")
        failures_by_group[(failure.configuration_id, failure.case_id)].append(failure)
    groups = []
    group_keys = set(grouped) | set(failures_by_group)
    for configuration, case_id in sorted(group_keys):
        runs = grouped.get((configuration, case_id), [])
        failures = failures_by_group.get((configuration, case_id), [])
        if any(run.attempt is not None and run.attempt > expected_repetitions for run in runs):
            raise ValueError("more attempts than the declared repetition budget")
        if runs and len({r.evidence_packet_sha256 for r in runs}) != 1:
            raise ValueError("repetitions must use the same Evidence Packet")
        if runs and len({r.task_context_sha256 for r in runs}) != 1:
            raise ValueError("repetitions must use the same pinned task context")
        if runs and len({r.adjudicator for r in runs}) != 1:
            raise ValueError("repetitions must use the same adjudicator")
        if len(runs) + len(failures) > expected_repetitions:
            raise ValueError("more attempts than the declared repetition budget")
        sets = [
            {
                g
                for d in r.adjudications
                if d.verdict == "matched"
                for g in (d.gold_id, *d.additional_gold_ids)
            }
            for r in runs
        ]
        intersection = set.intersection(*sets) if sets else set()
        union = set.union(*sets) if sets else set()
        pairs = [(a, b) for i, a in enumerate(sets) for b in sets[i + 1 :]]
        jaccard = [len(a & b) / len(a | b) for a, b in pairs if a | b]
        elapsed = sorted(r.elapsed_ms for r in runs)
        complete = len(runs) == expected_repetitions and not failures
        rows = [summaries[r.run_id] for r in runs]

        def spread(values):
            values = [v for v in values if v is not None]
            return (
                {"min": min(values), "max": max(values), "median": statistics.median(values)}
                if values
                else None
            )

        group = {
            "configuration_id": configuration,
            "case_id": case_id,
            "attempts": len(runs),
            "failed_attempts": len(failures),
            "attempts_recorded": len(runs) + len(failures),
            "expected_repetitions": expected_repetitions,
            "repetition_coverage_complete": complete,
            "always_found_gold": sorted(intersection),
            "ever_found_gold": sorted(union),
            "stable_gold_recall": len(intersection) / len(cases[case_id].gold)
            if complete and cases[case_id].gold_complete and cases[case_id].gold
            else None,
            "pairwise_gold_jaccard": statistics.mean(jaccard) if jaccard else None,
            "precision": spread(r["precision"] for r in rows),
            "recall": spread(r["recall"] for r in rows),
            "elapsed_ms": (
                {
                    **spread(elapsed),
                    "p95": elapsed[math.ceil(0.95 * len(elapsed)) - 1],
                }
                if elapsed
                else None
            ),
            "finding_identity": "explicit golden IDs only; valid_unlisted has no stable identity",
        }
        if failures:
            group["failures"] = [failure.model_dump(mode="json") for failure in failures]
        groups.append(group)
    configurations = sorted(
        {r.configuration_id for r in results.runs}
        | {failure.configuration_id for failure in results.failures}
    )
    payload = {
        "schema_version": 2 if results.failures else 1,
        "scoring": "local_explicit_adjudication_repetitions",
        "missing_cases_by_configuration": {
            c: sorted(set(cases) - {r.case_id for r in results.runs if r.configuration_id == c})
            for c in configurations
        },
        "groups": groups,
    }
    if results.failures:
        payload["failures"] = [failure.model_dump(mode="json") for failure in results.failures]
    return payload


def compare_speed(
    dataset: BenchmarkDataset,
    results: BenchmarkResults,
    *,
    baseline_configuration: str,
    candidate_configuration: str,
    repetitions: int,
    split: Literal["tune", "check"] | None = None,
) -> dict:
    """Compare two host configurations on identical pinned inputs and model settings."""
    if not 2 <= repetitions <= 20:
        raise ValueError("repetitions must be between 2 and 20")
    if baseline_configuration == candidate_configuration:
        raise ValueError("baseline and candidate configurations must differ")

    scored = score_benchmark(dataset, results, split=split)
    expected_cases = set(scored["dataset_cases"])
    if any(failure.case_id in expected_cases for failure in results.failures):
        raise ValueError(
            "speed comparison cannot include failed benchmark attempts in the selected split"
        )
    selected = tuple(
        run
        for run in results.runs
        if run.configuration_id in {baseline_configuration, candidate_configuration}
        and run.case_id in expected_cases
    )
    by_configuration: dict[str, dict[str, list[BenchmarkRun]]] = {
        baseline_configuration: defaultdict(list),
        candidate_configuration: defaultdict(list),
    }
    for run in selected:
        by_configuration[run.configuration_id][run.case_id].append(run)
    if not all(by_configuration.values()):
        raise ValueError("both benchmark configurations must have results")
    for cases in by_configuration.values():
        if set(cases) != expected_cases:
            raise ValueError("speed comparison requires complete coverage of the same dataset cases")
        if any(len(runs) != repetitions for runs in cases.values()):
            raise ValueError("each configuration and case must have exactly the declared repetitions")
        for runs in cases.values():
            if any(run.attempt is None for run in runs):
                raise ValueError("speed comparison requires explicit attempt numbers")
            if {run.attempt for run in runs} != set(range(1, repetitions + 1)):
                raise ValueError("each configuration and case must have attempts 1 through repetitions")

    def run_identity(run: BenchmarkRun) -> tuple:
        return (
            run.model,
            run.reasoning,
            run.checklist_sha256,
            run.host_instructions_sha256,
            run.review_rules_sha256,
            run.adjudicator,
        )

    baseline_runs = [run for cases in by_configuration[baseline_configuration].values() for run in cases]
    candidate_runs = [run for cases in by_configuration[candidate_configuration].values() for run in cases]
    if len({run_identity(run) for run in baseline_runs + candidate_runs}) != 1:
        raise ValueError(
            "speed comparison requires the same model, reasoning, review instructions and adjudicator"
        )

    schedule_assessment = "unknown"
    if results.schedule is not None:
        if results.schedule.strategy == "randomized":
            schedule_assessment = "randomized_host_declared"
        else:
            runs_by_id = {run.run_id: run for run in results.runs}
            selected_run_ids = {run.run_id for run in selected}
            ordered_pair = [
                runs_by_id[run_id]
                for run_id in results.schedule.run_order
                if run_id in selected_run_ids
            ]
            configurations_in_order = [run.configuration_id for run in ordered_pair]
            schedule_assessment = (
                "interleaved_validated"
                if len(ordered_pair) == len(baseline_runs) + len(candidate_runs)
                and all(left != right for left, right in pairwise(configurations_in_order))
                else "invalid"
            )

    def distribution(values: list[int], *, sample_count: int | None = None) -> dict:
        sample_count = len(values) if sample_count is None else sample_count
        ordered = sorted(values)
        return {
            "sample_count": sample_count,
            "observed_count": len(ordered),
            "missing_count": sample_count - len(ordered),
            "median": statistics.median(ordered) if ordered else None,
            "p95": ordered[math.ceil(0.95 * len(ordered)) - 1] if ordered else None,
        }

    per_case = []

    def median_usage(runs: list[BenchmarkRun], field: str) -> float | None:
        values = [getattr(run, field) for run in runs]
        return statistics.median(values) if all(value is not None for value in values) else None

    for case_id in sorted(expected_cases):
        baseline = by_configuration[baseline_configuration][case_id]
        candidate = by_configuration[candidate_configuration][case_id]
        baseline_packets = {run.evidence_packet_sha256 for run in baseline}
        candidate_packets = {run.evidence_packet_sha256 for run in candidate}
        if len(baseline_packets) != 1 or len(candidate_packets) != 1:
            raise ValueError("repetitions must use the same Evidence Packet within each configuration")
        if baseline_packets != candidate_packets:
            raise ValueError("speed comparison requires the same Evidence Packet across configurations")
        baseline_tasks = {run.task_context_sha256 for run in baseline}
        candidate_tasks = {run.task_context_sha256 for run in candidate}
        if len(baseline_tasks) != 1 or len(candidate_tasks) != 1 or baseline_tasks != candidate_tasks:
            raise ValueError("speed comparison requires the same task context per case")
        baseline_median = statistics.median(run.elapsed_ms for run in baseline)
        candidate_median = statistics.median(run.elapsed_ms for run in candidate)
        baseline_elapsed = distribution([run.elapsed_ms for run in baseline])
        candidate_elapsed = distribution([run.elapsed_ms for run in candidate])
        baseline_queue = distribution(
            [run.queue_wait_ms for run in baseline if run.queue_wait_ms is not None],
            sample_count=len(baseline),
        )
        candidate_queue = distribution(
            [run.queue_wait_ms for run in candidate if run.queue_wait_ms is not None],
            sample_count=len(candidate),
        )
        baseline_by_attempt = {run.attempt: run for run in baseline}
        candidate_by_attempt = {run.attempt: run for run in candidate}
        paired = [
            (baseline_by_attempt[attempt], candidate_by_attempt[attempt])
            for attempt in range(1, repetitions + 1)
        ]
        paired_speedups = [
            baseline_run.elapsed_ms / candidate_run.elapsed_ms
            for baseline_run, candidate_run in paired
            if candidate_run.elapsed_ms > 0
        ]
        paired_improvements = [
            (baseline_run.elapsed_ms - candidate_run.elapsed_ms)
            / baseline_run.elapsed_ms
            * 100
            for baseline_run, candidate_run in paired
            if baseline_run.elapsed_ms > 0
        ]
        baseline_input_tokens = median_usage(baseline, "input_tokens")
        candidate_input_tokens = median_usage(candidate, "input_tokens")
        input_token_savings_percent = (
            (baseline_input_tokens - candidate_input_tokens) / baseline_input_tokens * 100
            if baseline_input_tokens is not None
            and candidate_input_tokens is not None
            and baseline_input_tokens > 0
            else None
        )
        per_case.append(
            {
                "case_id": case_id,
                "repetitions": repetitions,
                "evidence_packet_sha256": next(iter(baseline_packets)),
                "task_context_sha256": next(iter(baseline_tasks)),
                "baseline_median_ms": baseline_median,
                "candidate_median_ms": candidate_median,
                "baseline_p95_elapsed_ms": baseline_elapsed["p95"],
                "candidate_p95_elapsed_ms": candidate_elapsed["p95"],
                "baseline_elapsed_sample_count": baseline_elapsed["sample_count"],
                "candidate_elapsed_sample_count": candidate_elapsed["sample_count"],
                "baseline_queue_wait_ms": baseline_queue,
                "candidate_queue_wait_ms": candidate_queue,
                "paired_repetitions": repetitions,
                "paired_median_speedup_ratio": (
                    statistics.median(paired_speedups) if paired_speedups else None
                ),
                "paired_median_improvement_percent": (
                    statistics.median(paired_improvements) if paired_improvements else None
                ),
                "baseline_median_input_tokens": baseline_input_tokens,
                "candidate_median_input_tokens": candidate_input_tokens,
                "input_token_savings_percent": input_token_savings_percent,
                "baseline_median_output_tokens": median_usage(baseline, "output_tokens"),
                "candidate_median_output_tokens": median_usage(candidate, "output_tokens"),
                "speedup_ratio": (
                    baseline_median / candidate_median if candidate_median > 0 else None
                ),
                "improvement_percent": (
                    (baseline_median - candidate_median) / baseline_median * 100
                    if baseline_median > 0
                    else None
                ),
            }
        )

    score = score_benchmark(
        dataset,
        BenchmarkResults(
            schema_version=results.schema_version,
            runs=tuple(baseline_runs + candidate_runs),
        ),
        split=split,
    )
    quality = {
        configuration: score["configurations"][configuration]
        for configuration in (baseline_configuration, candidate_configuration)
    }
    ratios = [case["speedup_ratio"] for case in per_case if case["speedup_ratio"] is not None]
    paired_ratios = [
        case["paired_median_speedup_ratio"]
        for case in per_case
        if case["paired_median_speedup_ratio"] is not None
    ]
    paired_improvements = [
        case["paired_median_improvement_percent"]
        for case in per_case
        if case["paired_median_improvement_percent"] is not None
    ]
    improvements = [
        case["improvement_percent"]
        for case in per_case
        if case["improvement_percent"] is not None
    ]
    baseline_input_total = (
        sum(run.input_tokens for run in baseline_runs)
        if all(run.input_tokens is not None for run in baseline_runs)
        else None
    )
    candidate_input_total = (
        sum(run.input_tokens for run in candidate_runs)
        if all(run.input_tokens is not None for run in candidate_runs)
        else None
    )
    baseline_output_total = (
        sum(run.output_tokens for run in baseline_runs)
        if all(run.output_tokens is not None for run in baseline_runs)
        else None
    )
    candidate_output_total = (
        sum(run.output_tokens for run in candidate_runs)
        if all(run.output_tokens is not None for run in candidate_runs)
        else None
    )
    baseline_elapsed = distribution([run.elapsed_ms for run in baseline_runs])
    candidate_elapsed = distribution([run.elapsed_ms for run in candidate_runs])
    baseline_queue = distribution(
        [run.queue_wait_ms for run in baseline_runs if run.queue_wait_ms is not None],
        sample_count=len(baseline_runs),
    )
    candidate_queue = distribution(
        [run.queue_wait_ms for run in candidate_runs if run.queue_wait_ms is not None],
        sample_count=len(candidate_runs),
    )
    return {
        "schema_version": 1,
        "comparison": "same_model_same_pinned_input_attempt_paired_host_elapsed_time",
        "dataset_kind": dataset.kind,
        "dataset_schema_version": dataset.schema_version,
        "dataset_split": split,
        "dataset_sha256": _benchmark_dataset_sha256(dataset),
        "baseline_configuration": baseline_configuration,
        "candidate_configuration": candidate_configuration,
        "dataset_cases": sorted(expected_cases),
        "model": baseline_runs[0].model,
        "reasoning": baseline_runs[0].reasoning,
        "repetitions_per_case": repetitions,
        "schedule_assessment": schedule_assessment,
        "schedule": (
            {
                "schedule_id": results.schedule.schedule_id,
                "strategy": results.schedule.strategy,
                "selected_run_count": len(baseline_runs) + len(candidate_runs),
            }
            if results.schedule is not None
            else None
        ),
        "cases": per_case,
        "aggregate": {
            "median_speedup_ratio": statistics.median(ratios) if ratios else None,
            "median_paired_speedup_ratio": (
                statistics.median(paired_ratios) if paired_ratios else None
            ),
            "median_paired_improvement_percent": (
                statistics.median(paired_improvements) if paired_improvements else None
            ),
            "median_improvement_percent": statistics.median(improvements) if improvements else None,
            "baseline_median_ms": statistics.median(
                case["baseline_median_ms"] for case in per_case
            ),
            "candidate_median_ms": statistics.median(
                case["candidate_median_ms"] for case in per_case
            ),
            "baseline_p95_elapsed_ms": baseline_elapsed["p95"],
            "candidate_p95_elapsed_ms": candidate_elapsed["p95"],
            "baseline_elapsed_sample_count": baseline_elapsed["sample_count"],
            "candidate_elapsed_sample_count": candidate_elapsed["sample_count"],
            "baseline_queue_wait_ms": baseline_queue,
            "candidate_queue_wait_ms": candidate_queue,
            "baseline_total_input_tokens": baseline_input_total,
            "candidate_total_input_tokens": candidate_input_total,
            "input_token_savings": (
                baseline_input_total - candidate_input_total
                if baseline_input_total is not None and candidate_input_total is not None
                else None
            ),
            "input_token_savings_percent": (
                (baseline_input_total - candidate_input_total) / baseline_input_total * 100
                if baseline_input_total is not None
                and candidate_input_total is not None
                and baseline_input_total > 0
                else None
            ),
            "baseline_total_output_tokens": baseline_output_total,
            "candidate_total_output_tokens": candidate_output_total,
        },
        "quality_by_configuration": quality,
        "timing_scope": (
            "host-reported elapsed_ms; callback must measure the same end-to-end scope. "
            "Attempts are paired by case and attempt number. p95 is descriptive at the reported "
            "sample count. Interleaved order is checked for alternating configurations; a "
            "randomized order remains host-declared and is not independently verified."
        ),
        "usage_scope": "host-reported token counters; missing values are not treated as zero",
    }


def evaluate_speed_candidate(
    comparison: Mapping[str, object], policy: BenchmarkQualityGatePolicy
) -> dict:
    """Decide whether a speed comparison is eligible under a pinned quality policy."""
    if not isinstance(policy, BenchmarkQualityGatePolicy):
        raise TypeError("policy must be a BenchmarkQualityGatePolicy")
    if not isinstance(comparison, Mapping):
        comparison = {}

    baseline_configuration = comparison.get("baseline_configuration")
    candidate_configuration = comparison.get("candidate_configuration")
    dataset_kind = comparison.get("dataset_kind")
    dataset_sha256 = comparison.get("dataset_sha256")
    if isinstance(dataset_sha256, str):
        dataset_sha256 = dataset_sha256.lower()
    schedule_assessment = comparison.get("schedule_assessment", "unknown")
    schedule = comparison.get("schedule")
    repetitions = comparison.get("repetitions_per_case")
    dataset_cases = comparison.get("dataset_cases")
    quality = comparison.get("quality_by_configuration")
    aggregate = comparison.get("aggregate")

    reasons: list[str] = []
    blocking: list[str] = []

    def block(reason: str) -> None:
        if reason not in reasons:
            reasons.append(reason)
        if reason not in blocking:
            blocking.append(reason)

    valid_configurations = (
        isinstance(baseline_configuration, str)
        and bool(baseline_configuration.strip())
        and isinstance(candidate_configuration, str)
        and bool(candidate_configuration.strip())
        and baseline_configuration != candidate_configuration
    )
    if not valid_configurations:
        block("baseline and candidate configuration identities are missing or invalid")
    if dataset_kind != policy.dataset_kind:
        block("comparison dataset kind does not match the policy")
    split_reason = _check_split_gate_reason(comparison)
    if split_reason is not None:
        block(split_reason)
    if dataset_sha256 != policy.dataset_sha256:
        block("comparison dataset SHA-256 does not match the policy-pinned dataset")
    if not isinstance(schedule_assessment, str) or schedule_assessment not in {
        "interleaved_validated",
        "randomized_host_declared",
    }:
        block(f"run schedule is {schedule_assessment}; controlled order is required")
    if policy.min_speed_repetitions is None:
        block("policy does not configure a minimum speed repetition count")
    if type(repetitions) is not int or not 2 <= repetitions <= 20:
        block("comparison repetition count is unavailable or invalid")
    elif (
        policy.min_speed_repetitions is not None
        and repetitions < policy.min_speed_repetitions
    ):
        block(
            f"comparison has {repetitions} repetitions per case; "
            f"{policy.min_speed_repetitions} required by policy"
        )

    if not isinstance(dataset_cases, list) or not dataset_cases or any(
        not isinstance(case_id, str) or not case_id.strip() for case_id in dataset_cases
    ):
        block("comparison dataset case identity is unavailable or malformed")
        dataset_cases = []
    elif len(set(dataset_cases)) != len(dataset_cases):
        block("comparison dataset case identity contains duplicates")

    if not isinstance(schedule, Mapping):
        block("schedule metadata is unavailable")
    else:
        strategy = schedule.get("strategy")
        schedule_id = schedule.get("schedule_id")
        selected_run_count = schedule.get("selected_run_count")
        if (
            not isinstance(schedule_id, str)
            or not schedule_id.strip()
            or not isinstance(strategy, str)
            or strategy not in {"interleaved", "randomized"}
            or type(selected_run_count) is not int
            or type(repetitions) is not int
            or selected_run_count != len(dataset_cases) * repetitions * 2
        ):
            block("schedule metadata is incomplete or inconsistent with the selected runs")
        elif (
            (schedule_assessment == "interleaved_validated" and strategy != "interleaved")
            or (
                schedule_assessment == "randomized_host_declared"
                and strategy != "randomized"
            )
        ):
            block("schedule assessment does not match the recorded schedule strategy")

    if not isinstance(aggregate, Mapping):
        block("aggregate speed measurements are unavailable")
    else:
        expected_samples = len(dataset_cases) * repetitions if type(repetitions) is int else 0
        for configuration, label in (
            (baseline_configuration, "baseline"),
            (candidate_configuration, "candidate"),
        ):
            sample_count = aggregate.get(f"{label}_elapsed_sample_count")
            median = aggregate.get(f"{label}_median_ms")
            p95 = aggregate.get(f"{label}_p95_elapsed_ms")
            if (
                not isinstance(configuration, str)
                or type(sample_count) is not int
                or sample_count != expected_samples
                or type(median) not in {int, float}
                or not math.isfinite(median)
                or median < 0
                or type(p95) is not int
                or p95 < 0
            ):
                block(f"{label} latency measurements are incomplete or invalid")
            queue = aggregate.get(f"{label}_queue_wait_ms")
            if not isinstance(queue, Mapping):
                block(f"{label} queue-wait sample counts are unavailable")
                continue
            observed_queue = queue.get("observed_count")
            missing_queue = queue.get("missing_count")
            queue_samples = queue.get("sample_count")
            queue_median = queue.get("median")
            queue_p95 = queue.get("p95")
            if (
                type(queue_samples) is not int
                or queue_samples != expected_samples
                or type(observed_queue) is not int
                or type(missing_queue) is not int
                or observed_queue < 0
                or missing_queue < 0
                or observed_queue + missing_queue != expected_samples
                or (
                    observed_queue == 0
                    and (queue_median is not None or queue_p95 is not None)
                )
                or (
                    observed_queue > 0
                    and (
                        type(queue_median) not in {int, float}
                        or not math.isfinite(queue_median)
                        or queue_median < 0
                        or type(queue_p95) not in {int, float}
                        or not math.isfinite(queue_p95)
                        or queue_p95 < 0
                    )
                )
            ):
                block(f"{label} queue-wait measurements are incomplete or invalid")

    quality_gates: dict[str, dict] = {}
    if not isinstance(quality, Mapping):
        block("quality evidence is unavailable for both configurations")
    elif not isinstance(dataset_cases, list):
        block("quality evidence cannot be bound to dataset cases")
    elif valid_configurations:
        for configuration, label in (
            (baseline_configuration, "baseline"),
            (candidate_configuration, "candidate"),
        ):
            aggregate_quality = quality.get(configuration)
            if not isinstance(aggregate_quality, Mapping):
                block(f"{label} quality evidence is unavailable")
                continue
            score = {
                "scoring": "local_explicit_adjudication",
                "dataset_kind": dataset_kind,
                "dataset_schema_version": comparison.get("dataset_schema_version"),
                "dataset_split": comparison.get("dataset_split"),
                "dataset_sha256": dataset_sha256,
                "dataset_cases": dataset_cases,
                "configurations": {configuration: dict(aggregate_quality)},
            }
            quality_gate = evaluate_quality_policy(
                score, policy, configuration_id=configuration
            )
            quality_gates[label] = quality_gate
            if label == "baseline" and quality_gate["state"] != "passed":
                block(f"baseline does not pass the quality policy ({quality_gate['state']})")
                reasons.extend(
                    f"baseline: {reason}"
                    for reason in quality_gate["reasons"]
                    if f"baseline: {reason}" not in reasons
                )
            elif label == "candidate" and quality_gate["state"] == "blocked":
                block("candidate quality evidence is blocked")
                reasons.extend(
                    f"candidate: {reason}"
                    for reason in quality_gate["reasons"]
                    if f"candidate: {reason}" not in reasons
                )
            elif label == "candidate" and quality_gate["state"] == "failed":
                reasons.extend(
                    f"candidate: {reason}"
                    for reason in quality_gate["reasons"]
                    if f"candidate: {reason}" not in reasons
                )

    if blocking:
        state = "blocked"
    elif quality_gates.get("candidate", {}).get("state") == "failed":
        state = "failed"
    elif (
        quality_gates.get("baseline", {}).get("state") == "passed"
        and quality_gates.get("candidate", {}).get("state") == "passed"
    ):
        state = "passed"
    else:
        state = "blocked"
        block("both baseline and candidate quality evidence are required")

    return {
        "schema_version": 1,
        "state": state,
        "policy_id": policy.policy_id,
        "dataset_kind": dataset_kind,
        "dataset_schema_version": comparison.get("dataset_schema_version"),
        "dataset_split": comparison.get("dataset_split"),
        "dataset_sha256": dataset_sha256,
        "baseline_configuration": baseline_configuration,
        "candidate_configuration": candidate_configuration,
        "schedule_assessment": schedule_assessment,
        "repetitions_per_case": repetitions,
        "min_speed_repetitions": policy.min_speed_repetitions,
        "baseline_quality": quality_gates.get("baseline"),
        "candidate_quality": quality_gates.get("candidate"),
        "reasons": reasons,
        "limitations": (
            ["randomized order is host-declared and was not independently verified"]
            if schedule_assessment == "randomized_host_declared"
            else []
        ),
    }


async def run_repetitions(
    dataset: BenchmarkDataset,
    execute: Callable[[BenchmarkCase, int], Awaitable[BenchmarkRun | BenchmarkFailure]],
    *,
    repetitions: int = 3,
) -> BenchmarkResults:
    """Fresh serial attempts; execute owns worker launch AND external adjudication.

    Return BenchmarkFailure for an expected failed attempt so the artifact preserves its
    classified diagnostics. Unexpected runner errors still abort; no hidden retries or reuse.
    """
    if not 2 <= repetitions <= 20:
        raise ValueError("repetitions must be between 2 and 20")
    runs = []
    failures = []
    for case in dataset.cases:
        for attempt in range(1, repetitions + 1):
            result = await execute(case, attempt)
            if isinstance(result, BenchmarkFailure):
                failure = result
                if failure.case_id != case.case_id or failure.attempt != attempt:
                    raise ValueError("repetition runner returned a different failure slot")
                failures.append(failure)
                continue
            run = BenchmarkRun.model_validate(result)
            if run.case_id != case.case_id:
                raise ValueError("repetition runner returned a different case")
            if run.attempt not in (None, attempt):
                raise ValueError("repetition runner returned a different run slot")
            if run.attempt is None:
                run = run.model_copy(update={"attempt": attempt})
            runs.append(run)
    has_provenance = any(
        decision.found_by is not None or decision.judged_by is not None
        for run in runs
        for decision in run.adjudications
    )
    results = BenchmarkResults(
        schema_version=5 if has_provenance else 2,
        runs=tuple(runs),
        failures=tuple(failures),
    )
    score_reliability(dataset, results, expected_repetitions=repetitions)
    return results


def import_reviewbench(golden_path: Path, repository_path: Path) -> BenchmarkDataset:
    try:
        return _import_reviewbench(golden_path, repository_path)
    except (KeyError, TypeError) as exc:
        raise ValueError("invalid ReviewBench golden document") from exc


def import_reviewbench_test_set(
    test_set_path: Path,
    findings_path: Path,
    repositories_root: Path,
    *,
    source_revision: str,
    source_checkout: Path | None = None,
) -> BenchmarkDataset:
    return import_reviewbench_case_set(
        test_set_path,
        findings_path,
        repositories_root,
        source_revision=source_revision,
        source_checkout=source_checkout,
    )


def import_reviewbench_case_set(
    tasks_path: Path,
    findings_path: Path,
    repositories_root: Path,
    *,
    source_revision: str,
    source_path: str = "corpus/test",
    source_checkout: Path | None = None,
) -> BenchmarkDataset:
    if (
        not isinstance(source_path, str)
        or not re.fullmatch(r"corpus(?:/[A-Za-z0-9_.-]+)*", source_path)
        or any(part in {".", ".."} for part in source_path.split("/"))
    ):
        raise ValueError("ReviewBench source path must be inside corpus")
    try:
        return _import_reviewbench_case_set(
            tasks_path,
            findings_path,
            repositories_root,
            source_revision=source_revision,
            source_path=source_path,
            source_checkout=source_checkout,
        )
    except (KeyError, TypeError) as exc:
        raise ValueError("invalid ReviewBench case-set documents") from exc


def _import_reviewbench_case_set(
    tasks_path: Path,
    findings_path: Path,
    repositories_root: Path,
    *,
    source_revision: str,
    source_path: str,
    source_checkout: Path | None,
) -> BenchmarkDataset:
    if not re.fullmatch(r"[0-9a-fA-F]{40}", source_revision):
        raise ValueError("ReviewBench source revision must be a full commit SHA")

    if source_checkout is None:
        tasks_raw = tasks_path.read_bytes()
        findings_raw = findings_path.read_bytes()
        schema_version = 2
        revision_status = "caller_supplied_unverified"
    else:
        tasks_raw, findings_raw = _read_verified_reviewbench_inputs(
            tasks_path,
            findings_path,
            source_checkout=source_checkout,
            source_revision=source_revision,
            source_path=source_path,
        )
        schema_version = 3
        revision_status = "checkout_verified"
    tasks = json.loads(tasks_raw)
    findings_by_case = json.loads(findings_raw)
    if not isinstance(tasks, list) or not tasks or not isinstance(findings_by_case, dict):
        raise ValueError("invalid ReviewBench case-set documents")

    case_ids = set()
    validated_tasks = []
    for task in tasks:
        nwo = task["nwo"]
        if not isinstance(nwo, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", nwo
        ):
            raise ValueError("invalid ReviewBench repository identity")
        owner, repository_name = nwo.split("/")
        if owner in {".", ".."} or repository_name in {".", ".."}:
            raise ValueError("invalid ReviewBench repository identity")
        pr_number = task["pr_number"]
        base_sha = task["base"]
        head_sha = task["head"]
        if type(pr_number) is not int or pr_number < 1:
            raise ValueError("invalid ReviewBench pull-request number")
        if not all(
            isinstance(revision, str) and re.fullmatch(r"[0-9a-fA-F]{40}", revision)
            for revision in (base_sha, head_sha)
        ):
            raise ValueError("ReviewBench base and head must be full commit SHAs")
        case_id = f"{owner}_{repository_name}_{pr_number}-{head_sha[:8]}"
        if case_id in case_ids:
            raise ValueError("duplicate ReviewBench test-set case")
        case_ids.add(case_id)
        if task["repo"] != f"https://github.com/{nwo}":
            raise ValueError("ReviewBench repository URL does not match its identity")
        pull_request_url = task.get("pr_url")
        if pull_request_url is not None and pull_request_url != (
            f"https://github.com/{nwo}/pull/{pr_number}"
        ):
            raise ValueError("ReviewBench pull-request URL does not match its identity")
        for field in ("title", "body", "language"):
            if task.get(field) is not None and not isinstance(task[field], str):
                raise ValueError(f"ReviewBench {field} must be a string")
        validated_tasks.append((case_id, task, owner, repository_name))

    missing_findings = sorted(case_ids - set(findings_by_case))
    if missing_findings:
        raise ValueError(f"missing ReviewBench findings for {missing_findings[0]}")
    if set(findings_by_case) != case_ids:
        raise ValueError("ReviewBench findings keys do not match the test-set cases")

    cases = []
    for case_id, task, owner, repository_name in validated_tasks:
        case_findings = findings_by_case[case_id]
        if not isinstance(case_findings, list):
            raise TypeError(f"ReviewBench findings for {case_id} must be a list")
        repository_path = repositories_root / f"{owner}_{repository_name}"
        if not repository_path.is_dir():
            raise ValueError(f"missing local ReviewBench mirror for {case_id}")
        cases.append(
            _build_reviewbench_case(
                case_id=case_id,
                repository=task["repo"],
                base_sha=task["base"],
                head_sha=task["head"],
                findings=case_findings,
                repository_path=repository_path,
                task_title=task.get("title"),
                task_body=task.get("body"),
                pull_request_url=task.get("pr_url"),
                language=task.get("language"),
            )
        )

    return BenchmarkDataset(
        schema_version=schema_version,
        kind="reviewbench_reference",
        source=(
            "https://github.com/review-bench/ReviewBench/tree/"
            f"{source_revision.lower()}/{source_path}"
        ),
        source_sha256=hashlib.sha256(tasks_raw + b"\0" + findings_raw).hexdigest(),
        source_revision_status=revision_status,
        cases=tuple(cases),
    )


def _read_verified_reviewbench_inputs(
    tasks_path: Path,
    findings_path: Path,
    *,
    source_checkout: Path,
    source_revision: str,
    source_path: str,
) -> tuple[bytes, bytes]:
    try:
        checkout = source_checkout.resolve(strict=True)
        if not checkout.is_dir():
            raise ValueError("ReviewBench source checkout must be a directory")
    except OSError as exc:
        raise ValueError("ReviewBench source checkout does not exist") from exc

    root_result = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "--show-toplevel"],
        capture_output=True,
        check=False,
        text=True,
    )
    if root_result.returncode:
        raise ValueError("ReviewBench source checkout is not a local Git checkout")
    root = Path(root_result.stdout.strip()).resolve()

    resolved_revision = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "rev-parse",
            "--verify",
            f"{source_revision.lower()}^{{commit}}",
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    if resolved_revision.returncode or resolved_revision.stdout.strip().lower() != (
        source_revision.lower()
    ):
        raise ValueError("ReviewBench source revision does not resolve to a commit")

    try:
        source_dir = (root / source_path).resolve(strict=True)
        source_dir_relative = source_dir.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ValueError("ReviewBench source path must be a directory inside the source checkout") from exc
    if not source_dir.is_dir():
        raise ValueError("ReviewBench source path must be a directory inside the source checkout")

    verified_inputs = []
    for input_path in (tasks_path, findings_path):
        if input_path.is_symlink():
            raise ValueError("ReviewBench inputs must not be symlinks")
        try:
            resolved_input = input_path.resolve(strict=True)
            input_relative = resolved_input.relative_to(root)
            input_relative.relative_to(source_dir_relative)
        except (OSError, ValueError) as exc:
            raise ValueError("ReviewBench inputs must be inside the source checkout path") from exc
        if not resolved_input.is_file():
            raise ValueError("ReviewBench inputs must be regular files in the source checkout")

        content = resolved_input.read_bytes()
        committed_blob = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "show",
                f"{source_revision.lower()}:{input_relative.as_posix()}",
            ],
            capture_output=True,
            check=False,
        )
        if committed_blob.returncode or committed_blob.stdout != content:
            raise ValueError("ReviewBench input does not match the committed ReviewBench source")
        verified_inputs.append(content)

    return verified_inputs[0], verified_inputs[1]


def _import_reviewbench(golden_path: Path, repository_path: Path) -> BenchmarkDataset:
    raw = golden_path.read_bytes()
    document = json.loads(raw)
    pr = document["pr"]
    # Validate revisions before passing them as git arguments.
    case = _build_reviewbench_case(
        case_id=document["pr_key"],
        repository=pr["repo"],
        base_sha=pr["base"],
        head_sha=pr["head"],
        findings=document["findings"],
        repository_path=repository_path,
        task_title=pr.get("title"),
        task_body=pr.get("body"),
        pull_request_url=pr.get("url"),
        language=pr.get("language"),
    )

    return BenchmarkDataset(
        kind="reviewbench_reference",
        source="https://github.com/review-bench/ReviewBench",
        source_sha256=hashlib.sha256(raw).hexdigest(),
        cases=(case,),
    )


def _build_reviewbench_case(
    *,
    case_id: str,
    repository: str,
    base_sha: str,
    head_sha: str,
    findings: list[dict],
    repository_path: Path,
    task_title: str | None = None,
    task_body: str | None = None,
    pull_request_url: str | None = None,
    language: str | None = None,
) -> BenchmarkCase:
    # Validate revisions before passing them as git arguments.
    case = BenchmarkCase(
        case_id=case_id,
        repository=repository,
        base_sha=base_sha,
        head_sha=head_sha,
        diff_sha256="0" * 64,
        gold=(),
    )
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repository_path),
            "diff",
            "--binary",
            "--no-ext-diff",
            "--no-textconv",
            case.base_sha,
            case.head_sha,
            "--",
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise ValueError("cannot read the pinned ReviewBench diff from the local repository")
    gold = tuple(
        GoldFinding(
            gold_id=f"G-{index:04d}",
            description=finding["message"],
            severity=finding["severity"],
            category=finding["category"],
            path=finding["file"],
            start_line=finding["start_line"],
            end_line=finding["end_line"],
            scope=finding.get("scope"),
            difficulty=finding.get("difficulty"),
            context_required=finding.get("context_required"),
        )
        for index, finding in enumerate(findings, start=1)
        if finding.get("tp_fp", "tp") == "tp"
    )
    return BenchmarkCase.model_validate(
        {
            **case.model_dump(mode="python"),
            "diff_sha256": hashlib.sha256(result.stdout).hexdigest(),
            "task_title": task_title,
            "task_body": task_body,
            "pull_request_url": pull_request_url,
            "language": language,
            "diff_bytes": len(result.stdout),
            "task_context_sha256": _task_context_sha256(task_title, task_body),
            "gold": gold,
            "gold_complete": True,
        }
    )


def _task_context_sha256(title: str | None, body: str | None) -> str | None:
    if title is None and body is None:
        return None
    context = json.dumps(
        {"title": title or "", "body": body or ""},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(context).hexdigest()


def benchmark_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="qa-orch benchmark")
    subparsers = parser.add_subparsers(dest="command", required=True)
    split = subparsers.add_parser(
        "split", help="Assign complete pull-request groups to tuning and check splits"
    )
    split.add_argument("--dataset", required=True, type=Path)
    split.add_argument("--output", required=True, type=Path)
    split.add_argument("--check-fraction", type=float, default=0.2)
    split.add_argument("--seed", default="0")
    score = subparsers.add_parser("score", help="Score pinned runs using explicit adjudications")
    score.add_argument("--dataset", required=True, type=Path)
    score.add_argument("--results", required=True, type=Path)
    score.add_argument("--split", choices=("tune", "check"))
    score.add_argument("--output", type=Path)
    quality_gate = subparsers.add_parser(
        "quality-gate",
        aliases=["gate"],
        help="Apply a pinned quality policy or experimental thresholds",
    )
    quality_gate.add_argument("--score", required=True, type=Path)
    quality_gate.add_argument("--configuration", required=True)
    quality_gate.add_argument("--policy", type=Path)
    quality_gate.add_argument("--min-cases", type=int)
    quality_gate.add_argument("--min-precision", type=float)
    quality_gate.add_argument("--min-recall", type=float)
    quality_gate.add_argument("--max-missed-high-impact", type=int)
    quality_gate.add_argument("--min-evidence-validation-rate", type=float)
    quality_gate.add_argument("--min-location-validation-rate", type=float)
    quality_gate.add_argument("--max-mean-elapsed-ms", type=float)
    quality_gate.add_argument("--max-p95-elapsed-ms", type=float)
    quality_gate.add_argument("--min-stratum-recall", type=float)
    quality_gate.add_argument("--min-stratum-gold-count", type=int)
    quality_gate.add_argument(
        "--require-dataset-kind", choices=("reviewbench_reference", "curated")
    )
    quality_gate.add_argument("--require-dataset-sha256")
    quality_gate.add_argument("--output", type=Path)
    reliability = subparsers.add_parser("reliability", help="Score fresh repeated review runs")
    reliability.add_argument("--dataset", required=True, type=Path)
    reliability.add_argument("--results", required=True, type=Path)
    reliability.add_argument("--repetitions", required=True, type=int)
    reliability.add_argument("--output", type=Path)
    compare = subparsers.add_parser(
        "compare", help="Compare host elapsed time on identical cases, packets and model settings"
    )
    compare.add_argument("--dataset", required=True, type=Path)
    compare.add_argument("--results", required=True, type=Path)
    compare.add_argument("--baseline", required=True)
    compare.add_argument("--candidate", required=True)
    compare.add_argument("--repetitions", required=True, type=int)
    compare.add_argument("--split", choices=("tune", "check"))
    compare.add_argument("--quality-policy", type=Path)
    compare.add_argument("--output", type=Path)
    prepare = subparsers.add_parser("import-reviewbench", help="Import one public golden case")
    prepare.add_argument("--golden", required=True, type=Path)
    prepare.add_argument("--repository", required=True, type=Path)
    prepare.add_argument("--output", required=True, type=Path)
    prepare_set = subparsers.add_parser(
        "import-reviewbench-set", help="Import a ReviewBench case-set dataset"
    )
    prepare_set.add_argument("--test-set", "--tasks", dest="test_set", required=True, type=Path)
    prepare_set.add_argument("--findings", required=True, type=Path)
    prepare_set.add_argument("--repositories-root", required=True, type=Path)
    prepare_set.add_argument(
        "--source-revision",
        required=True,
        help="Full ReviewBench commit SHA",
    )
    prepare_set.add_argument("--source-path", default="corpus/test")
    prepare_set.add_argument(
        "--source-checkout",
        type=Path,
        help="Local ReviewBench Git checkout; verifies the revision and exact input file blobs",
    )
    prepare_set.add_argument("--output", required=True, type=Path)
    diff_budget = subparsers.add_parser(
        "diff-budget", help="Flag raw diffs that exceed a supplied context byte budget"
    )
    diff_budget.add_argument("--dataset", required=True, type=Path)
    diff_budget.add_argument("--max-context-bytes", required=True, type=int)
    diff_budget.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.command in {"quality-gate", "gate"}:
        threshold_args = (
            args.min_cases,
            args.min_precision,
            args.min_recall,
            args.max_missed_high_impact,
            args.min_evidence_validation_rate,
            args.min_location_validation_rate,
            args.max_mean_elapsed_ms,
            args.max_p95_elapsed_ms,
            args.min_stratum_recall,
            args.min_stratum_gold_count,
            args.require_dataset_kind,
            args.require_dataset_sha256,
        )
        if args.policy:
            if any(value is not None for value in threshold_args):
                parser.error("--policy cannot be combined with experimental threshold arguments")
            score = json.loads(args.score.read_bytes())
            policy = BenchmarkQualityGatePolicy.model_validate_json(args.policy.read_bytes())
            payload = evaluate_quality_policy(
                score, policy, configuration_id=args.configuration
            )
        else:
            if args.min_precision is None or args.min_recall is None:
                parser.error(
                    "--min-precision and --min-recall are required without --policy"
                )
            score = json.loads(args.score.read_bytes())
            payload = evaluate_quality_gate(
                score,
                configuration_id=args.configuration,
                min_cases=args.min_cases if args.min_cases is not None else 1,
                min_precision=args.min_precision,
                min_recall=args.min_recall,
                max_missed_high_impact=(
                    args.max_missed_high_impact
                    if args.max_missed_high_impact is not None
                    else 0
                ),
                min_evidence_validation_rate=args.min_evidence_validation_rate,
                min_location_validation_rate=args.min_location_validation_rate,
                max_mean_elapsed_ms=args.max_mean_elapsed_ms,
                max_p95_elapsed_ms=args.max_p95_elapsed_ms,
                min_stratum_recall=args.min_stratum_recall,
                min_stratum_gold_count=(
                    args.min_stratum_gold_count
                    if args.min_stratum_gold_count is not None
                    else 5
                ),
                required_dataset_kind=args.require_dataset_kind,
                required_dataset_sha256=args.require_dataset_sha256,
            )
    elif args.command == "split":
        dataset = BenchmarkDataset.model_validate_json(args.dataset.read_bytes())
        payload = assign_benchmark_splits(
            dataset,
            check_fraction=args.check_fraction,
            seed=args.seed,
        ).model_dump(mode="json")
    elif args.command == "import-reviewbench":
        payload = import_reviewbench(args.golden, args.repository).model_dump(mode="json")
    elif args.command == "import-reviewbench-set":
        payload = import_reviewbench_case_set(
            args.test_set,
            args.findings,
            args.repositories_root,
            source_revision=args.source_revision,
            source_path=args.source_path,
            source_checkout=args.source_checkout,
        ).model_dump(mode="json")
    elif args.command == "diff-budget":
        dataset = BenchmarkDataset.model_validate_json(args.dataset.read_bytes())
        payload = check_diff_budget(dataset, max_context_bytes=args.max_context_bytes)
    else:
        dataset = BenchmarkDataset.model_validate_json(args.dataset.read_bytes())
        results = BenchmarkResults.model_validate_json(args.results.read_bytes())
        if args.command == "reliability":
            payload = score_reliability(dataset, results, expected_repetitions=args.repetitions)
        elif args.command == "compare":
            payload = compare_speed(
                dataset,
                results,
                baseline_configuration=args.baseline,
                candidate_configuration=args.candidate,
                repetitions=args.repetitions,
                split=args.split,
            )
            if args.quality_policy:
                policy = BenchmarkQualityGatePolicy.model_validate_json(
                    args.quality_policy.read_bytes()
                )
                payload["eligibility"] = evaluate_speed_candidate(payload, policy)
            else:
                payload["eligibility"] = "exploratory"
        else:
            payload = score_benchmark(dataset, results, split=args.split)
    output = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")
    if args.command in {"quality-gate", "gate"}:
        if "state" in payload:
            return 0 if payload["state"] == "passed" else 1
        return 0 if payload["passed"] else 1
    if args.command == "compare" and isinstance(payload.get("eligibility"), Mapping):
        return 0 if payload["eligibility"].get("state") == "passed" else 1
    return 0
