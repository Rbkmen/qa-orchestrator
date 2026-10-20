"""Local benchmark scoring from pinned inputs and explicit external adjudications."""

import argparse
import hashlib
import json
import math
import re
import statistics
import subprocess
from collections import defaultdict
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

from qa_orchestrator.contracts import GitObjectId, ReviewFindingCandidate, Sha256Digest
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
    language: str | None = Field(default=None, max_length=128)
    diff_bytes: int | None = Field(default=None, ge=0)
    task_context_sha256: Sha256Digest | None = None
    gold: tuple[GoldFinding, ...] = Field(max_length=1000)
    gold_complete: bool = False

    @model_serializer(mode="wrap")
    def serialize_optional_reviewbench_context(self, handler):
        payload = handler(self)
        for field in (
            "task_title", "task_body", "pull_request_url", "language", "diff_bytes",
            "task_context_sha256",
        ):
            if getattr(self, field) is None:
                payload.pop(field, None)
        return payload

    @field_validator("base_sha", "head_sha", "diff_sha256", "task_context_sha256")
    @classmethod
    def normalize_hashes(cls, value: str | None) -> str | None:
        return value.lower() if value is not None else None

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
        return self


class BenchmarkDataset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1, 2] = 1
    kind: Literal["synthetic", "reviewbench_reference", "curated"]
    source: str = Field(min_length=1, max_length=1024)
    source_sha256: Sha256Digest | None = None
    source_revision_status: Literal["caller_supplied_unverified"] | None = None
    cases: tuple[BenchmarkCase, ...] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def unique_cases(self) -> "BenchmarkDataset":
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("case IDs must be unique")
        if self.schema_version == 1 and self.source_revision_status is not None:
            raise ValueError("schema version 1 cannot declare source revision status")
        if self.schema_version == 2 and self.source_revision_status != "caller_supplied_unverified":
            raise ValueError("schema version 2 requires explicit source revision status")
        return self

    @model_serializer(mode="wrap")
    def serialize_revision_status(self, handler):
        payload = handler(self)
        if self.source_revision_status is None:
            payload.pop("source_revision_status", None)
        return payload


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
    attempt: int | None = Field(default=None, ge=1, le=20)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    candidates: tuple[ReviewFindingCandidate, ...] = Field(default=(), max_length=1000)
    adjudicator: str = Field(min_length=1, max_length=256)
    adjudications: tuple[CandidateAdjudication, ...] = Field(default=(), max_length=1000)

    @model_serializer(mode="wrap")
    def serialize_optional_task_context(self, handler):
        payload = handler(self)
        if self.task_context_sha256 is None:
            payload.pop("task_context_sha256", None)
        if self.attempt is None:
            payload.pop("attempt", None)
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
        return self


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


class BenchmarkResults(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1, 2] = 1
    runs: tuple[BenchmarkRun, ...] = Field(default=(), max_length=10000)
    failures: tuple[BenchmarkFailure, ...] = Field(default=(), max_length=10000)

    @model_validator(mode="after")
    def unique_runs(self) -> "BenchmarkResults":
        if not self.runs and not self.failures:
            raise ValueError("benchmark results must contain a run or a failure")
        if self.schema_version == 1 and (
            self.failures or any(run.attempt is not None for run in self.runs)
        ):
            raise ValueError("schema version 1 cannot contain attempt diagnostics")
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


def score_benchmark(dataset: BenchmarkDataset, results: BenchmarkResults) -> dict:
    cases = {case.case_id: case for case in dataset.cases}
    summaries = []
    failures_by_configuration = defaultdict(list)
    for failure in results.failures:
        if failure.case_id not in cases:
            raise ValueError("benchmark failure references an unknown dataset case")
        failures_by_configuration[failure.configuration_id].append(failure)
    for run in results.runs:
        case = cases.get(run.case_id)
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
        runs_by_configuration[run.configuration_id].append(run)
    aggregates = {}
    configurations = set(by_configuration) | set(failures_by_configuration)
    for configuration in sorted(configurations):
        rows = by_configuration.get(configuration, [])
        count = sum(r["candidates"] for r in rows)
        missing_cases = sorted(set(cases) - {r["case_id"] for r in rows})
        gold_count = sum(r["gold_count"] for r in rows)
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
            "unverified_candidates": sum(r["unverified"] for r in rows),
            "precision": sum(r["valid"] for r in rows) / count if count else None,
            "recall": sum(r["matched_gold"] for r in rows) / gold_count
            if not missing_cases and gold_count and all(r["recall"] is not None for r in rows)
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
        if failures_by_configuration.get(configuration):
            aggregates[configuration]["failed_attempts"] = len(
                failures_by_configuration[configuration]
            )
    payload = {
        "schema_version": 2 if results.failures else 1,
        "scoring": "local_explicit_adjudication",
        "dataset_kind": dataset.kind,
        "dataset_cases": sorted(cases),
        "runs": summaries,
        "configurations": aggregates,
    }
    if results.failures:
        payload["failures"] = [failure.model_dump(mode="json") for failure in results.failures]
    return payload


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
        "configuration_id": configuration_id,
        "dataset_kind": score.get("dataset_kind"),
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
        },
        "observed": observed,
        "reasons": reasons,
        "scope": (
            "Checks aggregate metrics from explicit host/reviewer adjudications. It cannot "
            "verify adjudicator correctness or prove that a model executed the review."
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
) -> dict:
    """Compare two host configurations on identical pinned inputs and model settings."""
    if not 2 <= repetitions <= 20:
        raise ValueError("repetitions must be between 2 and 20")
    if baseline_configuration == candidate_configuration:
        raise ValueError("baseline and candidate configurations must differ")
    if results.failures:
        raise ValueError("speed comparison cannot include failed benchmark attempts")

    score_benchmark(dataset, results)
    selected = tuple(
        run
        for run in results.runs
        if run.configuration_id in {baseline_configuration, candidate_configuration}
    )
    by_configuration: dict[str, dict[str, list[BenchmarkRun]]] = {
        baseline_configuration: defaultdict(list),
        candidate_configuration: defaultdict(list),
    }
    for run in selected:
        by_configuration[run.configuration_id][run.case_id].append(run)
    if not all(by_configuration.values()):
        raise ValueError("both benchmark configurations must have results")
    expected_cases = {case.case_id for case in dataset.cases}
    for cases in by_configuration.values():
        if set(cases) != expected_cases:
            raise ValueError("speed comparison requires complete coverage of the same dataset cases")
        if any(len(runs) != repetitions for runs in cases.values()):
            raise ValueError("each configuration and case must have exactly the declared repetitions")

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
    )
    quality = {
        configuration: {
            key: score["configurations"][configuration][key]
            for key in ("precision", "recall", "precision_is_lower_bound", "coverage_complete")
        }
        for configuration in (baseline_configuration, candidate_configuration)
    }
    ratios = [case["speedup_ratio"] for case in per_case if case["speedup_ratio"] is not None]
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
    return {
        "schema_version": 1,
        "comparison": "same_model_same_pinned_input_host_elapsed_time",
        "baseline_configuration": baseline_configuration,
        "candidate_configuration": candidate_configuration,
        "model": baseline_runs[0].model,
        "reasoning": baseline_runs[0].reasoning,
        "repetitions_per_case": repetitions,
        "cases": per_case,
        "aggregate": {
            "median_speedup_ratio": statistics.median(ratios) if ratios else None,
            "median_improvement_percent": statistics.median(improvements) if improvements else None,
            "baseline_median_ms": statistics.median(
                case["baseline_median_ms"] for case in per_case
            ),
            "candidate_median_ms": statistics.median(
                case["candidate_median_ms"] for case in per_case
            ),
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
        "timing_scope": "host-reported elapsed_ms; callback must measure the same end-to-end scope",
        "usage_scope": "host-reported token counters; missing values are not treated as zero",
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
    results = BenchmarkResults(schema_version=2, runs=tuple(runs), failures=tuple(failures))
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
) -> BenchmarkDataset:
    return import_reviewbench_case_set(
        test_set_path,
        findings_path,
        repositories_root,
        source_revision=source_revision,
    )


def import_reviewbench_case_set(
    tasks_path: Path,
    findings_path: Path,
    repositories_root: Path,
    *,
    source_revision: str,
    source_path: str = "corpus/test",
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
) -> BenchmarkDataset:
    if not re.fullmatch(r"[0-9a-fA-F]{40}", source_revision):
        raise ValueError("ReviewBench source revision must be a full commit SHA")

    tasks_raw = tasks_path.read_bytes()
    findings_raw = findings_path.read_bytes()
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
        schema_version=2,
        kind="reviewbench_reference",
        source=(
            "https://github.com/review-bench/ReviewBench/tree/"
            f"{source_revision.lower()}/{source_path}"
        ),
        source_sha256=hashlib.sha256(tasks_raw + b"\0" + findings_raw).hexdigest(),
        source_revision_status="caller_supplied_unverified",
        cases=tuple(cases),
    )


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
    score = subparsers.add_parser("score", help="Score pinned runs using explicit adjudications")
    score.add_argument("--dataset", required=True, type=Path)
    score.add_argument("--results", required=True, type=Path)
    score.add_argument("--output", type=Path)
    quality_gate = subparsers.add_parser(
        "quality-gate", aliases=["gate"], help="Apply explicit quality and speed thresholds"
    )
    quality_gate.add_argument("--score", required=True, type=Path)
    quality_gate.add_argument("--configuration", required=True)
    quality_gate.add_argument("--min-cases", default=1, type=int)
    quality_gate.add_argument("--min-precision", required=True, type=float)
    quality_gate.add_argument("--min-recall", required=True, type=float)
    quality_gate.add_argument("--max-missed-high-impact", default=0, type=int)
    quality_gate.add_argument("--min-evidence-validation-rate", type=float)
    quality_gate.add_argument("--min-location-validation-rate", type=float)
    quality_gate.add_argument("--max-mean-elapsed-ms", type=float)
    quality_gate.add_argument("--max-p95-elapsed-ms", type=float)
    quality_gate.add_argument("--min-stratum-recall", type=float)
    quality_gate.add_argument("--min-stratum-gold-count", default=5, type=int)
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
        help="Full commit SHA supplied by caller; not verified against the checkout",
    )
    prepare_set.add_argument("--source-path", default="corpus/test")
    prepare_set.add_argument("--output", required=True, type=Path)
    diff_budget = subparsers.add_parser(
        "diff-budget", help="Flag raw diffs that exceed a supplied context byte budget"
    )
    diff_budget.add_argument("--dataset", required=True, type=Path)
    diff_budget.add_argument("--max-context-bytes", required=True, type=int)
    diff_budget.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.command in {"quality-gate", "gate"}:
        score = json.loads(args.score.read_bytes())
        payload = evaluate_quality_gate(
            score,
            configuration_id=args.configuration,
            min_cases=args.min_cases,
            min_precision=args.min_precision,
            min_recall=args.min_recall,
            max_missed_high_impact=args.max_missed_high_impact,
            min_evidence_validation_rate=args.min_evidence_validation_rate,
            min_location_validation_rate=args.min_location_validation_rate,
            max_mean_elapsed_ms=args.max_mean_elapsed_ms,
            max_p95_elapsed_ms=args.max_p95_elapsed_ms,
            min_stratum_recall=args.min_stratum_recall,
            min_stratum_gold_count=args.min_stratum_gold_count,
        )
    elif args.command == "import-reviewbench":
        payload = import_reviewbench(args.golden, args.repository).model_dump(mode="json")
    elif args.command == "import-reviewbench-set":
        payload = import_reviewbench_case_set(
            args.test_set,
            args.findings,
            args.repositories_root,
            source_revision=args.source_revision,
            source_path=args.source_path,
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
            )
        else:
            payload = score_benchmark(dataset, results)
    output = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")
    return 1 if args.command in {"quality-gate", "gate"} and not payload["passed"] else 0
