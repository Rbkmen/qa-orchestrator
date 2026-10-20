# Review Verification Provenance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Show verification results as current, stale, host-reported, locally reproduced, or provider-verified without overstating what the orchestrator checked.

**Architecture:** Extend ReviewVerificationRecord additively, normalize a provider response into a typed receipt through a provider-neutral verifier boundary, and derive SHA binding in the report. A stale result remains visible but cannot count as a pass for the reviewed head.

**Tech Stack:** Python 3.12, Pydantic 2, pytest; no new runtime dependencies.

**Spec:** docs/superpowers/specs/2026-10-20-review-verification-provenance-design.md

## Global Constraints

- Existing records load as host-reported; never upgrade legacy claims to verified.
- A current result must match ReviewContext.head_sha.
- Provider verification requires a typed verifier receipt; a URL or passed status is insufficient.
- Store hashes and references only, never credentials or raw logs.
- Provider adapters remain read-only and outside this implementation.
- Use CodeGraph before changing contract/report/adapter modules and after the render stage to inspect all serialization and report call paths.

## Review Focus

- Passed result from a different commit: show stale and exclude it from current passing verification.
- Old record with no provenance: show host-reported/unverified.
- Provider claims only a conclusion but wrong run ID, SHA, scope, or time: refuse a verified receipt.
- Local run with no artifact digest: do not call it locally reproduced.
- Runtime evidence ref points to another revision: preserve it only when it matches the verification record SHA; never count it as current proof.

---

### Task 1: Extend verification contracts

**Files:**
- Modify: src/qa_orchestrator/contracts.py
- Test: tests/test_orchestration_contracts.py

**Interfaces:**
- Add VerificationReceipt with verifier_id, run_id, head_sha, conclusion, scope_sha256, source_response_sha256, and verified_at.
- Add optional scope, scope_sha256, run_id, artifact_sha256, provenance, and receipt fields to ReviewVerificationRecord.
- Legacy records default to host_reported. provider_verified requires a matching receipt plus scope, run ID, and timestamps; local_reproduced requires local_runner, scope, run ID, and an artifact digest.

- [ ] **Step 1: Write failing model tests**

~~~python
def test_provider_verified_record_requires_matching_receipt():
    with pytest.raises(ValidationError):
        ReviewVerificationRecord.model_validate(provider_verified_record_without_receipt())

def test_provider_verified_record_accepts_matching_verifier_receipt():
    record = ReviewVerificationRecord.model_validate(provider_verified_record_with_matching_receipt())
    assert record.provenance == "provider_verified"

def test_legacy_verification_record_defaults_to_host_reported():
    record = ReviewVerificationRecord.model_validate(legacy_verification_record())
    assert record.provenance == "host_reported"

def test_local_reproduction_requires_artifact_digest():
    with pytest.raises(ValidationError):
        ReviewVerificationRecord.model_validate(local_record_without_artifact_digest())

def test_local_reproduction_requires_run_identity_and_scope():
    with pytest.raises(ValidationError):
        ReviewVerificationRecord.model_validate(local_record_without_run_id_or_scope())
~~~

- [ ] **Step 2: Run the focused tests and confirm they fail**

Run: uv run pytest tests/test_orchestration_contracts.py::test_provider_verified_record_requires_matching_receipt tests/test_orchestration_contracts.py::test_provider_verified_record_accepts_matching_verifier_receipt tests/test_orchestration_contracts.py::test_legacy_verification_record_defaults_to_host_reported tests/test_orchestration_contracts.py::test_local_reproduction_requires_artifact_digest tests/test_orchestration_contracts.py::test_local_reproduction_requires_run_identity_and_scope -q

Expected: provenance and receipt fields are not defined.

- [ ] **Step 3: Implement VerificationReceipt and the additive record fields**

Add cross-field validation for run ID, head SHA, conclusion, scope digest, and provenance. Preserve existing timestamps, source fields, and error reasons.

- [ ] **Step 4: Re-run the focused tests**

Run: uv run pytest tests/test_orchestration_contracts.py::test_provider_verified_record_requires_matching_receipt tests/test_orchestration_contracts.py::test_provider_verified_record_accepts_matching_verifier_receipt tests/test_orchestration_contracts.py::test_legacy_verification_record_defaults_to_host_reported tests/test_orchestration_contracts.py::test_local_reproduction_requires_artifact_digest tests/test_orchestration_contracts.py::test_local_reproduction_requires_run_identity_and_scope -q

Expected: invalid claims fail and legacy records remain host-reported.

### Task 2: Normalize provider results through a generic verifier boundary

**Files:**
- Create: src/qa_orchestrator/verification.py
- Test: tests/test_verification.py

Test imports: add datetime and timedelta from datetime; helper response builders live in tests/test_verification.py.

**Interfaces:**
- Implement verify_provider_run(record: ReviewVerificationRecord, response: Mapping[str, object], *, verified_at: datetime) -> VerificationReceipt.
- The normalized response contains only run ID, head SHA, conclusion, scope digest, and start/completion timestamps; reject unexpected fields. Return a receipt only when all values match the record and verified_at is at or after completion. The function performs no network call; only a configured read-only provider adapter may supply provider-fetched data for provider_verified provenance.

- [ ] **Step 1: Write mismatch and matching-response tests**

~~~python
def test_verify_provider_run_rejects_wrong_sha():
    with pytest.raises(ValueError, match="head SHA"):
        verify_provider_run(record, provider_response(head_sha="f" * 40), verified_at=now)

def test_verify_provider_run_rejects_scope_or_timestamp_mismatch():
    with pytest.raises(ValueError):
        verify_provider_run(record, provider_response(scope_sha256="0" * 64), verified_at=now)

def test_verify_provider_run_rejects_completion_after_verified_at():
    with pytest.raises(ValueError, match="verified_at"):
        verify_provider_run(record, provider_response_matching(record), verified_at=record.completed_at - timedelta(seconds=1))

def test_verify_provider_run_returns_receipt_for_matching_run():
    receipt = verify_provider_run(record, provider_response_matching(record), verified_at=now)
    assert receipt.head_sha == record.head_sha
    assert receipt.run_id == record.run_id
~~~

- [ ] **Step 2: Run the focused tests and confirm they fail**

Run: uv run pytest tests/test_verification.py -q

Expected: module and verifier function are not defined.

- [ ] **Step 3: Implement the provider-neutral verifier**

Validate the response shape and comparisons; never perform network calls or accept secrets in the response. Return only bounded metadata and hashes.

- [ ] **Step 4: Re-run the focused tests**

Run: uv run pytest tests/test_verification.py -q

Expected: mismatches are rejected and only matching data yields a receipt.

### Task 3: Render current and stale verification states

**Files:**
- Modify: src/qa_orchestrator/report.py
- Test: tests/test_report.py
- Test: tests/test_host_adapter.py

**Interfaces:**
- Add verification_binding(record: ReviewVerificationRecord, context: ReviewContext) -> Literal["current", "stale"].
- Pass context into _render_verification_records. For runtime evidence, require its revision to match the record head; keep provider verification separate from host-reported and local reproduction.

- [ ] **Step 1: Update the exact-head test to require visible stale status**

Update test_verification_records_are_bound_to_exact_head_and_rendered so a wrong-head record renders as stale and cannot appear as a current pass. Add assertions for host-reported and provider-verified labels. Update test_verification_evidence_requires_head_bound_runtime_record to preserve an older run only when its evidence ref has that same older revision, and assert it cannot count as current proof. Add coverage that failed, skipped, not-run, cancelled, and unknown records keep their reason and never count as a current pass.

- [ ] **Step 2: Run the focused test and confirm the stale assertion fails**

Run: uv run pytest tests/test_report.py::test_verification_records_are_bound_to_exact_head_and_rendered tests/test_report.py::test_verification_evidence_requires_head_bound_runtime_record -q

Expected: wrong-head records are rejected before they can be rendered with a stale label.

- [ ] **Step 3: Implement binding-aware rendering and validation**

Keep evidence refs constrained to the bound context. Permit a stale verification record to render with an explicit stale marker, but do not let its status satisfy current passing verification. A stale runtime evidence ref is accepted only when its registry revision equals that record's head SHA. Show provenance, scope, run identity, and artifact digest when present.

- [ ] **Step 4: Run report and host-adapter tests**

Run: uv run pytest tests/test_report.py tests/test_host_adapter.py -q

Expected: stale results stay visible and existing atomic Markdown export remains valid.
