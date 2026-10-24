# Prior Finding Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing final omission audit challenge every prior finding candidate before the host relies on it.

**Architecture:** Extend only the `OMISSION_AUDITOR` profile instructions and required report sections. Preserve the current route topology and host-owned decisions.

**Tech Stack:** Python 3.12+, pytest, Ruff.

**Spec:** `docs/superpowers/specs/2026-10-24-prior-finding-validation-design.md`

## Global Constraints

- Do not add a profile, execution group, provider, dependency, or model call.
- Require `supported`, `refuted`, or `unverified` disposition per prior candidate.
- Keep omission and removed-behavior audits intact.
- Do not present model-reported dispositions as independently verified truth.

## Review Focus

- Every earlier implementation candidate must receive exactly one disposition.
- Unavailable or insufficient evidence must remain `unverified`.
- A refuted candidate must include concrete counterevidence.
- Validation must not silently remove or rewrite earlier candidates.
- Existing omission-audit requirements and bundle order must remain unchanged.

## Task 1: Add prior-finding validation contract

**Files:** `src/qa_orchestrator/review_profiles.py`, `tests/test_review_profiles.py`, `docs/REVIEW_UPGRADES.md`

- [x] Add profile assertions and observe the expected missing-section failure.
- [x] Add the `Prior Finding Validation` required section and evidence-grounded disposition instructions to the existing omission auditor.
- [x] Document the pass's scope and semantic-verification limit.
- [x] Run `./.venv/bin/pytest -q tests/test_review_profiles.py` — 18 passed.
- [x] Run `./.venv/bin/pytest -q` — 723 passed.
- [x] Run `./.venv/bin/ruff check .` and `git diff --check` — both clean.

## Final review

- [x] Confirm the existing execution groups are unchanged.
- [x] Confirm earlier user changes remain present in the diff.
- [x] Do not commit or push unless separately requested.
