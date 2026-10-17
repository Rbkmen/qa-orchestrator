"""Local benchmark scoring from pinned inputs and explicit external adjudications."""

import argparse
import hashlib
import json
import math
import statistics
import subprocess
from collections import defaultdict
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from qa_orchestrator.contracts import GitObjectId, ReviewFindingCandidate, Sha256Digest


class GoldFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    gold_id: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=20000)
    severity: Literal["high", "medium", "low"]
    category: str = Field(min_length=1, max_length=128)
    path: str = Field(min_length=1, max_length=1024)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

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
    gold: tuple[GoldFinding, ...] = Field(max_length=1000)
    gold_complete: bool = False

    @field_validator("base_sha", "head_sha", "diff_sha256")
    @classmethod
    def normalize_hashes(cls, value: str) -> str:
        return value.lower()

    @model_validator(mode="after")
    def unique_gold(self) -> "BenchmarkCase":
        if self.base_sha == self.head_sha:
            raise ValueError("benchmark revisions must differ")
        if len({finding.gold_id for finding in self.gold}) != len(self.gold):
            raise ValueError("gold IDs must be unique within a case")
        return self


class BenchmarkDataset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    kind: Literal["synthetic", "reviewbench_reference", "curated"]
    source: str = Field(min_length=1, max_length=1024)
    source_sha256: Sha256Digest | None = None
    cases: tuple[BenchmarkCase, ...] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def unique_cases(self) -> "BenchmarkDataset":
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("case IDs must be unique")
        return self


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
    base_sha: GitObjectId
    head_sha: GitObjectId
    diff_sha256: Sha256Digest
    evidence_packet_sha256: Sha256Digest
    elapsed_ms: int = Field(ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    candidates: tuple[ReviewFindingCandidate, ...] = Field(default=(), max_length=1000)
    adjudicator: str = Field(min_length=1, max_length=256)
    adjudications: tuple[CandidateAdjudication, ...] = Field(default=(), max_length=1000)

    @field_validator(
        "base_sha",
        "head_sha",
        "diff_sha256",
        "evidence_packet_sha256",
        "checklist_sha256",
        "host_instructions_sha256",
        "review_rules_sha256",
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


class BenchmarkResults(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    runs: tuple[BenchmarkRun, ...] = Field(min_length=1, max_length=10000)

    @model_validator(mode="after")
    def unique_runs(self) -> "BenchmarkResults":
        if len({run.run_id for run in self.runs}) != len(self.runs):
            raise ValueError("run IDs must be unique")
        configurations = {}
        for run in self.runs:
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


def score_benchmark(dataset: BenchmarkDataset, results: BenchmarkResults) -> dict:
    cases = {case.case_id: case for case in dataset.cases}
    summaries = []
    for run in results.runs:
        case = cases.get(run.case_id)
        if case is None or (case.base_sha, case.head_sha, case.diff_sha256) != (
            run.base_sha,
            run.head_sha,
            run.diff_sha256,
        ):
            raise ValueError("benchmark run does not match a pinned dataset case")
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
    for summary in summaries:
        by_configuration[summary["configuration_id"]].append(summary)
    aggregates = {}
    for configuration, rows in by_configuration.items():
        count = sum(r["candidates"] for r in rows)
        missing_cases = sorted(set(cases) - {r["case_id"] for r in rows})
        gold_count = sum(r["gold_count"] for r in rows)
        aggregates[configuration] = {
            "runs": len(rows),
            "cases": sorted({r["case_id"] for r in rows}),
            "missing_cases": missing_cases,
            "coverage_complete": not missing_cases,
            "precision": sum(r["valid"] for r in rows) / count if count else None,
            "recall": sum(r["matched_gold"] for r in rows) / gold_count
            if not missing_cases and gold_count and all(r["recall"] is not None for r in rows)
            else None,
            "precision_is_lower_bound": any(r["unverified"] for r in rows),
            "mean_elapsed_ms": sum(r["elapsed_ms"] for r in rows) / len(rows),
            "input_tokens": sum(r["input_tokens"] for r in rows)
            if all(r["input_tokens"] is not None for r in rows)
            else None,
            "output_tokens": sum(r["output_tokens"] for r in rows)
            if all(r["output_tokens"] is not None for r in rows)
            else None,
        }
    return {
        "schema_version": 1,
        "scoring": "local_explicit_adjudication",
        "dataset_kind": dataset.kind,
        "dataset_cases": sorted(cases),
        "runs": summaries,
        "configurations": aggregates,
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
    groups = []
    for (configuration, case_id), runs in sorted(grouped.items()):
        if len({r.evidence_packet_sha256 for r in runs}) != 1:
            raise ValueError("repetitions must use the same Evidence Packet")
        if len(runs) > expected_repetitions:
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
        intersection, union = set.intersection(*sets), set.union(*sets)
        pairs = [(a, b) for i, a in enumerate(sets) for b in sets[i + 1 :]]
        jaccard = [len(a & b) / len(a | b) for a, b in pairs if a | b]
        elapsed = sorted(r.elapsed_ms for r in runs)
        complete = len(runs) == expected_repetitions
        rows = [summaries[r.run_id] for r in runs]

        def spread(values):
            values = [v for v in values if v is not None]
            return (
                {"min": min(values), "max": max(values), "median": statistics.median(values)}
                if values
                else None
            )

        groups.append(
            {
                "configuration_id": configuration,
                "case_id": case_id,
                "attempts": len(runs),
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
                "elapsed_ms": {
                    **spread(elapsed),
                    "p95": elapsed[math.ceil(0.95 * len(elapsed)) - 1],
                },
                "finding_identity": "explicit golden IDs only; valid_unlisted has no stable identity",
            }
        )
    return {
        "schema_version": 1,
        "scoring": "local_explicit_adjudication_repetitions",
        "missing_cases_by_configuration": {
            c: sorted(set(cases) - {r.case_id for r in results.runs if r.configuration_id == c})
            for c in sorted({r.configuration_id for r in results.runs})
        },
        "groups": groups,
    }


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
    for case_id in sorted(expected_cases):
        baseline = by_configuration[baseline_configuration][case_id]
        candidate = by_configuration[candidate_configuration][case_id]
        baseline_packets = {run.evidence_packet_sha256 for run in baseline}
        candidate_packets = {run.evidence_packet_sha256 for run in candidate}
        if len(baseline_packets) != 1 or len(candidate_packets) != 1:
            raise ValueError("repetitions must use the same Evidence Packet within each configuration")
        if baseline_packets != candidate_packets:
            raise ValueError("speed comparison requires the same Evidence Packet across configurations")
        baseline_median = statistics.median(run.elapsed_ms for run in baseline)
        candidate_median = statistics.median(run.elapsed_ms for run in candidate)
        per_case.append(
            {
                "case_id": case_id,
                "repetitions": repetitions,
                "evidence_packet_sha256": next(iter(baseline_packets)),
                "baseline_median_ms": baseline_median,
                "candidate_median_ms": candidate_median,
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

    score = score_benchmark(dataset, BenchmarkResults(runs=tuple(baseline_runs + candidate_runs)))
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
        },
        "quality_by_configuration": quality,
        "timing_scope": "host-reported elapsed_ms; callback must measure the same end-to-end scope",
    }


async def run_repetitions(
    dataset: BenchmarkDataset,
    execute: Callable[[BenchmarkCase, int], Awaitable[BenchmarkRun]],
    *,
    repetitions: int = 3,
) -> BenchmarkResults:
    """Fresh serial attempts; execute owns worker launch AND external adjudication.

    Failed calls abort; no duplication of prior results, hidden retries or checkpoint reuse.
    """
    if not 2 <= repetitions <= 20:
        raise ValueError("repetitions must be between 2 and 20")
    runs = []
    for case in dataset.cases:
        for attempt in range(1, repetitions + 1):
            run = BenchmarkRun.model_validate(await execute(case, attempt))
            if run.case_id != case.case_id:
                raise ValueError("repetition runner returned a different case")
            runs.append(run)
    results = BenchmarkResults(runs=tuple(runs))
    score_reliability(dataset, results, expected_repetitions=repetitions)
    return results


def import_reviewbench(golden_path: Path, repository_path: Path) -> BenchmarkDataset:
    try:
        return _import_reviewbench(golden_path, repository_path)
    except (KeyError, TypeError) as exc:
        raise ValueError("invalid ReviewBench golden document") from exc


def _import_reviewbench(golden_path: Path, repository_path: Path) -> BenchmarkDataset:
    raw = golden_path.read_bytes()
    document = json.loads(raw)
    pr = document["pr"]
    # Validate revisions before passing them as git arguments.
    case = BenchmarkCase(
        case_id=document["pr_key"],
        repository=pr["repo"],
        base_sha=pr["base"],
        head_sha=pr["head"],
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
        )
        for index, finding in enumerate(document["findings"], start=1)
        if finding["tp_fp"] == "tp"
    )
    return BenchmarkDataset(
        kind="reviewbench_reference",
        source="https://github.com/review-bench/ReviewBench",
        source_sha256=hashlib.sha256(raw).hexdigest(),
        cases=(
            case.model_copy(
                update={
                    "diff_sha256": hashlib.sha256(result.stdout).hexdigest(),
                    "gold": gold,
                    "gold_complete": True,
                }
            ),
        ),
    )


def benchmark_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="qa-orch benchmark")
    subparsers = parser.add_subparsers(dest="command", required=True)
    score = subparsers.add_parser("score", help="Score pinned runs using explicit adjudications")
    score.add_argument("--dataset", required=True, type=Path)
    score.add_argument("--results", required=True, type=Path)
    score.add_argument("--output", type=Path)
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
    args = parser.parse_args(argv)
    if args.command == "import-reviewbench":
        payload = import_reviewbench(args.golden, args.repository).model_dump(mode="json")
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
    return 0
