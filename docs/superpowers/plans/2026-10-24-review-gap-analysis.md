# Review Gap Analysis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close specific evidence and post-run analysis gaps in code review without adding background work or external providers.

**Architecture:** Strengthen the existing omission-auditor route, feed bounded removed-diff anchors into existing neighbor discovery, and add an optional post-run benchmark diagnosis model. Gold comparison remains outside blind review and existing benchmark result formats remain readable.

**Tech Stack:** Python 3.12+, Pydantic, pytest, Ruff.

**Spec:** `docs/superpowers/specs/2026-10-24-review-gap-analysis-design.md`

## Global Constraints

- Do not add CI or scheduled jobs, dependencies, providers, or model calls.
- Keep neighbor discovery bounded and report additions/removals separately.
- Never infer why a finding was missed; diagnoses are explicit human adjudications only.
- Keep gold and miss diagnoses out of blind review inputs.
- Preserve old benchmark result payloads and scoring semantics.

## Review Focus

- Removed test-only identifiers must not seed production neighbor discovery.
- A removed anchor with no matching consumer must not fabricate a candidate.
- Incomplete gold must not be represented as complete recall or receive a miss diagnosis.
- Diagnoses must refer only to known, unmatched gold IDs and must not change precision/recall.
- Existing benchmark schemas and schedule metadata must remain compatible.

---

### Task 1: Removed/replaced behavior audit

**Files:** `src/qa_orchestrator/review_profiles.py`, `tests/test_review_profiles.py`

- [x] Add a failing route-contract test for a dedicated `Removed Behavior Audit` section and wording that requires old behavior, consumers, replacement or absence, and evidence/tests.
- [x] Run the focused test and observe the expected failure (2 expected assertion failures before implementation).
- [x] Update only the omission-auditor focus and required sections. Keep unconfirmed missing replacements as coverage gaps, not automatic findings.
- [x] Run `./.venv/bin/pytest -q tests/test_review_profiles.py` — 18 passed; `git diff --check` clean.

### Task 2: Removed diff anchors in neighbor discovery

**Files:** `src/qa_orchestrator/evidence.py`, `tests/test_evidence.py`

- [x] Add a failing test for finding consumers of removed behavior anchors in a pinned synthetic repository whose paths do not overlap the changed path.
- [x] Run the focused test and observe the expected failure: deleted anchors were absent from the discovery result.
- [x] Reuse the existing bounded term extraction and relationship expansion for removed lines; retain added/removed anchor lists in the discovery payload and use their union for searching.
- [x] Cap each side at 128 distinct terms of at most 256 UTF-8 bytes and expose omitted-term counts in the bound omission list.
- [x] Run `./.venv/bin/pytest -q tests/test_evidence.py` — 73 passed; consumer retrieval, test-path exclusion, and anchor caps/omissions are covered.

### Task 3: Actionable post-run missed-gold analysis

**Files:** `src/qa_orchestrator/benchmark.py`, `tests/test_benchmark.py`, `docs/REVIEW_UPGRADES.md`

- [x] Add failing tests for detailed unmatched-gold output, optional explicit cause/rationale annotations, unknown/matched/incomplete-gold rejection, legacy compatibility, and unchanged quality metrics.
- [x] Add a backward-compatible optional diagnosis field to benchmark runs and require result schema version 4 when it is used; allow existing schedule/failure metadata in version 4.
- [x] Emit sorted unmatched-gold metadata, completeness, and an explicit `unclassified` state when no manual diagnosis exists. Do not use diagnoses to change metrics.
- [x] Document the post-run workflow and the four cause classes.
- [x] Run `./.venv/bin/pytest -q tests/test_benchmark.py` — 65 passed; v1 omission and v4 schedule/failure serialization covered.

### Final verification

- [x] Run the full suite with `./.venv/bin/pytest -q` — 723 passed.
- [x] Run `./.venv/bin/ruff check .` and `git diff --check` — both clean.
- [x] Re-read the diff and confirm no provider call, CI/job change, gold-to-review leakage, or unbounded search was introduced.
- [x] Do not commit or push; no delivery request was included.
