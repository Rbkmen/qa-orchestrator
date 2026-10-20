# Strict Review Completion Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Prevent a new bound review from being finalized as `completed` when declared inputs or bounded impact candidates are unknown, partial, mismatched, or unresolved.

**Architecture:** New bound sessions use strict completion semantics. Persisted sessions without the new mode remain legacy-compatible. A shared evidence helper derives completion gaps from the input manifest and impact ledger; finish and session validation both enforce it.

**Tech Stack:** Python 3.12, Pydantic 2, pytest; no new runtime dependencies.

**Spec:** `docs/superpowers/specs/2026-10-20-review-scope-completeness-design.md`

**Ruling:** Keep old persisted sessions in `legacy` mode by default, but mark every newly started bound session `strict`. This preserves recovery compatibility while preventing new bound reviews from claiming completion with unknown scope.

## Global Constraints

- Missing legacy metadata remains parseable and is never upgraded to complete.
- A strict `completed` outcome requires complete required-input metadata and a closed ledger bound to the same head, changed paths, and discovery digest; any ledger evidence refs must belong to the bound packet.
- `partial` and `blocked` outcomes remain available when coverage is incomplete.
- Closure remains scoped to the recorded bounded discovery; it is not exhaustive repository coverage.
- No external provider reads, credentials, benchmark thresholds, or adjudicated corpus are introduced.

## Review Focus

- Missing manifest or zero required sources: strict completion is rejected as unknown.
- Required source has incomplete retrieval, pagination, or extraction: strict completion is rejected as partial.
- Registry contains a requirement or claim missing from manifest `criterion_refs`: input completeness is partial and the omitted refs are rendered.
- Impact ledger is absent, partial, capped, unresolved, or references a different head/diff/discovery: strict completion is rejected.
- Legacy serialized sessions omit completion mode: they continue to load and replay under legacy semantics.
- Partial/blocked outcomes with incomplete evidence remain finalizable.

## Task 1: Add completion-gate regression tests

**Files:** `tests/test_orchestration.py`, `tests/test_orchestration_contracts.py`, `tests/test_report.py`.

- [x] Add `test_new_bound_session_uses_strict_review_completion_mode` and parameterized `test_strict_bound_completion_rejects_unknown_or_incomplete_coverage` for unknown/partial inputs and missing/partial/mismatched impact ledgers.
- [x] Add `test_strict_bound_completion_accepts_complete_matching_ledgers`, `test_incomplete_bound_review_can_finish_as_partial_or_blocked`, `test_partial_bound_review_rejects_impact_ledger_for_another_discovery`, and `test_legacy_completed_session_without_completion_mode_loads_as_legacy`.
- [x] Add `test_manifest_without_registered_requirement_is_partial` so registered criteria omitted from the manifest cannot be called complete.
- [x] Run focused tests and observe the expected failures before implementing the gate.

**Verification:** `uv run pytest tests/test_orchestration.py tests/test_orchestration_contracts.py tests/test_report.py -q` and the full suite.

## Task 2: Implement strict and legacy completion modes

**Files:** `src/qa_orchestrator/orchestration.py`, `src/qa_orchestrator/evidence.py`, `src/qa_orchestrator/report.py`, orchestration/report tests and their bound-review fixtures.

- [x] Add `review_completion_mode: Literal["legacy", "strict"]` to `QaOrchestrationSession`, defaulting to `legacy` for old persisted payloads; set it to `strict` when a new session starts with a bound `ReviewContext`.
- [x] Add shared helpers for required-input completeness, impact-ledger presence, binding, and closed status; reuse them from report-context validation and orchestration completion checks.
- [x] Reject strict `completed` outcomes with actionable gap details and reject invalid persisted strict-completed sessions during model validation.
- [x] Update focused tests and bound-review fixtures without weakening existing assertions.

**Verification:** `uv run pytest tests/test_orchestration.py tests/test_orchestration_contracts.py tests/test_report.py -q`.

## Task 3: Explain the new completion contract to clients

**Files:** `client-rules/generic/QA_ORCHESTRATOR_INSTRUCTIONS.md`, `docs/ORCHESTRATION_POLICY.md`.

- [x] State that new bound sessions require a complete required-input manifest and a closed, matching impact ledger before `completed`; use `partial` or `blocked` when either is unknown or incomplete.
- [x] Preserve the explicit bounded-discovery limitation.
- [x] Run the full test suite, Ruff, and `git diff --check`; inspect the final diff and verify only planned files changed.

**Verification:** `uv run pytest -q`, `uv run ruff check .`, `git diff --check`.
