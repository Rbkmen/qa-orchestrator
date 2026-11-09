# Review Gap Analysis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close specific evidence and post-run analysis gaps in code review without adding background work or external providers.

**Architecture:** Strengthen the existing omission-auditor route, feed bounded removed-diff anchors into existing neighbor discovery,.

**Tech Stack:** Python 3.12+, Pydantic, pytest, Ruff.

**Spec:** `docs/superpowers/specs/2026-10-24-review-gap-analysis-design.md`

## Global Constraints

- Do not add CI or scheduled jobs, dependencies, providers, or model calls.
- Keep neighbor discovery bounded and report additions/removals separately.

## Review Focus

- Removed test-only identifiers must not seed production neighbor discovery.
- A removed anchor with no matching consumer must not fabricate a candidate.

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

### Final verification

- [x] Run the full suite with `./.venv/bin/pytest -q` — 723 passed.
- [x] Run `./.venv/bin/ruff check .` and `git diff --check` — both clean.
- [x] Do not commit or push; no delivery request was included.
