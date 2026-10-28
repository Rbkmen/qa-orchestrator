# PR-Scoped Benchmark Holdout Splits Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deterministic tune/check workflow that keeps pull-request cases together and prevents quality gates from passing on tuning data.

**Architecture:** Extend the existing benchmark case and dataset models with optional split metadata and schema version 4. Add a deterministic split command, split-aware scoring, and a gate check that requires the `check` split for schema version 4 while leaving legacy datasets unchanged.

**Tech Stack:** Python 3.12+, Pydantic, argparse, pytest, Ruff.

**Spec:** `docs/superpowers/specs/2026-10-28-benchmark-heldout-splits-design.md`

## Global Constraints

- Preserve support for dataset schema versions 1 through 3 and score their current behavior.
- Bind score output to the digest of the complete dataset, even when scoring one split.
- Never mark synthetic or unadjudicated findings as representative recall evidence.
- Require same-PR cases to share one split and group identity.

## Review Focus

- Same pull request represented by more than one case must never cross splits; test URL and explicit group identity conflicts.
- A case with no URL or explicit group identity cannot be split; test the clear failure path.
- Small datasets must contain at least one group in each split; test invalid group counts and split fractions.
- Scoring only `check` must calculate coverage against check cases and retain the full dataset digest; test with mixed result records.
- Schema versions 1 through 3 and old score documents must remain compatible; run the existing benchmark suite.

---

### Task 1: Add the PR-scoped split workflow

**Files:**
- Modify: `src/qa_orchestrator/benchmark.py`
- Test: `tests/test_benchmark.py`
- Modify: `docs/BENCHMARK.md`
- Modify: `README.md`

**Interfaces:**
- Produces `assign_benchmark_splits(dataset: BenchmarkDataset, *, check_fraction: float = 0.2, seed: str = "0") -> BenchmarkDataset`.
- Produces `score_benchmark(dataset: BenchmarkDataset, results: BenchmarkResults, *, split: Literal["tune", "check"] | None = None) -> dict`.
- Adds optional `split: Literal["tune", "check"] | None` to `compare_speed`.
- The CLI adds `qa-orch benchmark split --dataset PATH --output PATH [--check-fraction FLOAT] [--seed TEXT]`, `qa-orch benchmark score ... [--split tune|check]`, and `qa-orch benchmark compare ... [--split tune|check]`.
- Schema version 4 requires `source_revision_status` to be either `caller_supplied_unverified` or `checkout_verified`, and every case to have both a split and a stable group ID.

- [x] **Step 1: Write failing tests**
  - Test deterministic seeded assignment over whole pull-request groups, including two cases for one PR.
  - Test missing group identity and fewer than two groups are rejected.
  - Test conflicting split/group metadata for cases sharing a pull-request URL is rejected.
  - Test split scoring filters runs and coverage to the selected cases while reporting the full dataset digest.
  - Test quality gates block schema-version-4 scores scored on `tune` or all cases, and accept a valid `check` score, including the paired speed policy.
  - Test CLI split output and `--split check` scoring.

- [x] **Step 2: Run focused tests to verify they fail**

  Run: `PYTHONPATH=src /Users/andreiviarshko/Projects/qa-orchestrator/.venv/bin/pytest tests/test_benchmark.py -q`

  Expected: new tests fail because split metadata, commands, and gate enforcement are not implemented.

- [x] **Step 3: Implement the model, deterministic assignment, CLI, scoring, and gate checks**

  Keep the split assignment based on whole groups and stable SHA-256 ordering. Validate the complete dataset before scoring and use only selected cases for per-split coverage and aggregates.

- [x] **Step 4: Run focused tests to verify they pass**

  Run: `PYTHONPATH=src /Users/andreiviarshko/Projects/qa-orchestrator/.venv/bin/pytest tests/test_benchmark.py -q`

  Expected: all benchmark tests pass, including the new split behavior.

- [x] **Step 5: Document the workflow and run full verification**

  Run: `PYTHONPATH=src /Users/andreiviarshko/Projects/qa-orchestrator/.venv/bin/pytest -q`

  Expected: full suite passes. Also run Ruff and `git diff --check`.

- [x] **Step 6: Commit the verified change**

  Commit with a message describing the PR-scoped benchmark split workflow.
