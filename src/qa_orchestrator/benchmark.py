"""Local benchmark scoring from pinned inputs and explicit external adjudications."""

import argparse
import hashlib
import json
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    rationale: str = Field(min_length=1, max_length=5000)
    evidence_valid: bool | None = None
    location_valid: bool | None = None

    @model_validator(mode="after")
    def match_requires_gold(self) -> "CandidateAdjudication":
        if (self.verdict in {"matched", "duplicate"}) != (self.gold_id is not None):
            raise ValueError("only matched and duplicate verdicts require gold_id")
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

    @model_validator(mode="after")
    def all_candidates_adjudicated(self) -> "BenchmarkRun":
        candidate_ids = [candidate.candidate_id for candidate in self.candidates]
        decision_ids = [decision.candidate_id for decision in self.adjudications]
        if len(set(candidate_ids)) != len(candidate_ids):
            raise ValueError("benchmark candidate IDs must be unique")
        if len(set(decision_ids)) != len(decision_ids) or set(candidate_ids) != set(decision_ids):
            raise ValueError("exactly one adjudication is required for every candidate")
        matched = [d.gold_id for d in self.adjudications if d.verdict == "matched"]
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
        if any(d.gold_id is not None and d.gold_id not in gold for d in run.adjudications):
            raise ValueError("adjudication cites an unknown gold finding")
        matched = {d.gold_id for d in run.adjudications if d.verdict == "matched"}
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
        payload = score_benchmark(dataset, results)
    output = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")
    return 0
