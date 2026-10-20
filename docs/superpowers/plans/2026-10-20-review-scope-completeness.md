# Review Scope Completeness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Show whether declared review inputs and discovered blast-radius candidates were actually covered.

**Architecture:** Add a host-authored source manifest to ReviewContext and an explicit impact ledger to FinalReviewReport. Derive completeness from those pinned records, validate them against the bound evidence, and render partial/unknown states without claiming exhaustive discovery.

**Tech Stack:** Python 3.12, Pydantic 2, pytest; no new runtime dependencies.

**Spec:** docs/superpowers/specs/2026-10-20-review-scope-completeness-design.md

## Global Constraints

- Keep existing serialized ReviewContext and FinalReviewReport inputs readable.
- Missing legacy metadata is unknown, never complete.
- Do not store raw Jira, MR, specification, or source contents in the new metadata.
- Impact closure covers only the bounded discovery result and must say so.
- Preserve exact repository, head SHA, changed-path digest, and evidence-reference binding.
- Keep each stage small and run its focused tests before starting the next stage.

## Review Focus

- Required source has zero criteria but pagination or extraction is incomplete: input state remains partial.
- Manifest criterion reference is absent from the bound requirement/claim registry: preserve it and render a visible coverage gap.
- Complete source legitimately contains zero criteria: it may be complete without inventing a criterion.
- CodeGraph is disabled, stale, unavailable, or capped: impact state remains partial or unknown.
- Ledger is replayed against another head, changed-path set, or discovery output: context validation rejects it.

---

### Task 1: Add the review-input manifest contract

**Files:**
- Modify: src/qa_orchestrator/contracts.py
- Test: tests/test_orchestration_contracts.py

**Interfaces:**
- Produces ReviewInputSource, ReviewInputManifest, and optional ReviewContext.input_manifest for the remaining tasks.
- Each source has source_id, kind, locator, required, optional snapshot digest/revision, retrieval status (`complete`, `partial`, `failed`, `unknown`), pagination status (`complete`, `incomplete`, `not_applicable`, `unknown`), extraction status (`complete`, `partial`, `failed`, `unknown`), and criterion_refs.
- Manifest source IDs and criterion_refs are unique. If a criterion ref resolves in the registry, its kind must be requirement or claim; an unresolved ref is retained for Task 2 to report as a gap.

- [ ] **Step 1: Write the failing contract tests**

~~~python
def test_review_input_manifest_rejects_duplicate_source_ids_and_criteria_refs():
    with pytest.raises(ValidationError):
        ReviewContext.model_validate(context_payload_with_duplicate_manifest_refs())

def test_legacy_review_context_without_input_manifest_round_trips():
    context = ReviewContext.model_validate(legacy_context_payload())
    assert context.input_manifest is None
~~~

- [ ] **Step 2: Run the focused tests and confirm they fail**

Run: uv run pytest tests/test_orchestration_contracts.py::test_review_input_manifest_rejects_duplicate_source_ids_and_criteria_refs tests/test_orchestration_contracts.py::test_legacy_review_context_without_input_manifest_round_trips -q

Expected: the new field and validation are missing.

- [ ] **Step 3: Implement the manifest models in src/qa_orchestrator/contracts.py**

Add the named models and ReviewContext field; validate stable locators, unique IDs, allowed states, and any resolvable registry linkage. Preserve unresolved criterion refs for report gaps. Do not add a separate criteria system.

- [ ] **Step 4: Re-run the focused tests**

Run: uv run pytest tests/test_orchestration_contracts.py::test_review_input_manifest_rejects_duplicate_source_ids_and_criteria_refs tests/test_orchestration_contracts.py::test_legacy_review_context_without_input_manifest_round_trips -q

Expected: both tests pass and legacy JSON omits the absent optional field.

### Task 2: Derive and render input completeness

**Files:**
- Modify: src/qa_orchestrator/evidence.py
- Modify: src/qa_orchestrator/report.py
- Test: tests/test_report.py

**Interfaces:**
- Consumes ReviewContext.input_manifest from Task 1.
- Produces input_manifest_completeness(context: ReviewContext) -> dict with status, source counts, and explicit incomplete source/criterion references.
- Status is unknown when context/manifest is absent or no required sources are declared. Incomplete optional sources are listed but do not make required-input status partial.

- [ ] **Step 1: Write tests for complete, partial, and unknown states**

~~~python
def test_report_renders_incomplete_required_input_as_partial(bound_review_context):
    rendered = render_final_report(report, context=context_with_partial_input_manifest())
    assert "Input completeness: partial" in rendered
    assert "JIRA-1" in rendered

def test_report_marks_missing_input_manifest_unknown(bound_review_context):
    rendered = render_final_report(report, context=bound_review_context)
    assert "Input completeness: unknown" in rendered

def test_report_without_context_marks_input_completeness_unknown():
    rendered = render_final_report(report)
    assert "Input completeness: unknown" in rendered

def test_report_lists_manifest_criterion_missing_from_registry(bound_review_context):
    summary = input_manifest_completeness(context_with_unregistered_criterion(bound_review_context))
    assert summary["missing_criterion_refs"] == ["R-2"]

def test_empty_criteria_can_be_complete_after_complete_extraction(bound_review_context):
    summary = input_manifest_completeness(context_with_complete_empty_criteria(bound_review_context))
    assert summary["status"] == "complete"
~~~

- [ ] **Step 2: Run the focused tests and confirm they fail**

Run: uv run pytest tests/test_report.py::test_report_renders_incomplete_required_input_as_partial tests/test_report.py::test_report_marks_missing_input_manifest_unknown tests/test_report.py::test_report_without_context_marks_input_completeness_unknown tests/test_report.py::test_report_lists_manifest_criterion_missing_from_registry tests/test_report.py::test_empty_criteria_can_be_complete_after_complete_extraction -q

Expected: the report has no input-completeness section.

- [ ] **Step 3: Implement input_manifest_completeness and render its result**

In evidence.py, derive complete only when all required retrieval, pagination, and extraction states are complete; treat not-applicable pagination as complete only when declared. Preserve empty criteria as valid. In report.py, render unknown, partial, or complete with counts and source identifiers.

- [ ] **Step 4: Re-run the focused tests**

Run: uv run pytest tests/test_report.py::test_report_renders_incomplete_required_input_as_partial tests/test_report.py::test_report_marks_missing_input_manifest_unknown tests/test_report.py::test_report_without_context_marks_input_completeness_unknown tests/test_report.py::test_report_lists_manifest_criterion_missing_from_registry tests/test_report.py::test_empty_criteria_can_be_complete_after_complete_extraction -q

Expected: both states and their source details are visible.

### Task 3: Add impact dispositions and bind the ledger

**Files:**
- Modify: src/qa_orchestrator/contracts.py
- Modify: src/qa_orchestrator/report.py
- Test: tests/test_orchestration_contracts.py

**Interfaces:**
- Produces ImpactCandidateDisposition and ImpactCoverageLedger.
- FinalReviewReport.impact_coverage is optional for compatibility.
- Candidate disposition values are reviewed, no_relevant_relationship, unresolved, and not_checked; ledger binds head_sha, changed_paths_sha256, discovery_sha256, discovery state, limits, and candidates.

- [ ] **Step 1: Write failing ledger validation tests**

~~~python
def test_impact_coverage_rejects_duplicate_candidates_and_invalid_dispositions():
    with pytest.raises(ValidationError):
        ImpactCoverageLedger.model_validate(ledger_with_duplicate_candidate_ids())

def test_final_report_accepts_absent_legacy_impact_coverage():
    report = FinalReviewReport.model_validate(legacy_report_payload())
    assert report.impact_coverage is None
~~~

- [ ] **Step 2: Run the focused tests and confirm they fail**

Run: uv run pytest tests/test_orchestration_contracts.py::test_impact_coverage_rejects_duplicate_candidates_and_invalid_dispositions tests/test_orchestration_contracts.py::test_final_report_accepts_absent_legacy_impact_coverage -q

Expected: impact-coverage contract is not defined.

- [ ] **Step 3: Implement the impact models and FinalReviewReport field**

Keep limits and omissions explicit; require rationale for each disposition and unique evidence refs. Do not let a ledger claim exhaustive repository coverage.

- [ ] **Step 4: Re-run the focused tests**

Run: uv run pytest tests/test_orchestration_contracts.py::test_impact_coverage_rejects_duplicate_candidates_and_invalid_dispositions tests/test_orchestration_contracts.py::test_final_report_accepts_absent_legacy_impact_coverage -q

Expected: invalid ledgers fail and old reports remain parseable.

### Task 4: Fingerprint bounded discovery output

**Files:**
- Modify: src/qa_orchestrator/evidence.py
- Test: tests/test_evidence.py

**Interfaces:**
- Produces neighbor_discovery_sha256(payload: Mapping[str, object]) -> str.
- discover_neighbor_evidence adds the digest over its canonical candidates, omissions, scan limits, bound head, and changed paths; the digest does not include itself.

- [ ] **Step 1: Write the deterministic-digest test**

~~~python
def test_discovery_digest_is_stable_and_changes_when_omissions_change(discovery_payload):
    first = neighbor_discovery_sha256(discovery_payload)
    assert first == neighbor_discovery_sha256(discovery_payload)
    assert first != neighbor_discovery_sha256(payload_with_one_new_omission(discovery_payload))
~~~

- [ ] **Step 2: Run the focused test and confirm it fails**

Run: uv run pytest tests/test_evidence.py::test_discovery_digest_is_stable_and_changes_when_omissions_change -q

Expected: neighbor_discovery_sha256 is not defined.

- [ ] **Step 3: Implement canonical discovery fingerprinting**

Use deterministic JSON ordering and SHA-256. Add the result to the existing discover payload without changing its advisory/non-exhaustive meaning.

- [ ] **Step 4: Re-run the focused test and existing discovery boundary tests**

Run: uv run pytest tests/test_evidence.py::test_discovery_digest_is_stable_and_changes_when_omissions_change tests/test_evidence.py::test_discovery_reports_unavailable_optional_codegraph tests/test_evidence.py::test_discovery_skips_symbol_search_when_neighbor_file_exceeds_byte_cap -q

Expected: stable fingerprints and unchanged omission behavior.

### Task 5: Validate and render impact closure

**Files:**
- Modify: src/qa_orchestrator/report.py
- Test: tests/test_report.py
- Test: tests/test_orchestration.py

**Interfaces:**
- Consumes ImpactCoverageLedger from Task 3 and discovery digest from Task 4.
- report validation compares ledger head, changed-path digest, discovery digest, and evidence refs against ReviewContext.
- Renderer shows counts, limits, omissions, and partial/unknown/closed disposition.

- [ ] **Step 1: Write tests for stale binding and incomplete discovery**

~~~python
def test_impact_ledger_rejects_wrong_head_or_discovery_digest(bound_review_context):
    with pytest.raises(ValueError, match="impact coverage"):
        validate_final_report_context(report_with_wrong_impact_digest(), bound_review_context)

def test_report_never_calls_capped_discovery_closed(bound_review_context):
    rendered = render_final_report(report_with_capped_impact_ledger(), context=bound_review_context)
    assert "Impact closure: partial" in rendered
    assert "capped" in rendered

def test_impact_rationale_is_markdown_escaped(bound_review_context):
    rendered = render_final_report(report_with_hostile_impact_rationale(), context=bound_review_context)
    assert "<script>" not in rendered

def test_impact_locator_is_markdown_escaped(bound_review_context):
    rendered = render_final_report(report_with_hostile_impact_locator(), context=bound_review_context)
    assert "<script>" not in rendered
~~~

- [ ] **Step 2: Run the focused tests and confirm they fail**

Run: uv run pytest tests/test_report.py::test_impact_ledger_rejects_wrong_head_or_discovery_digest tests/test_report.py::test_report_never_calls_capped_discovery_closed tests/test_report.py::test_impact_rationale_is_markdown_escaped tests/test_report.py::test_impact_locator_is_markdown_escaped -q

Expected: no impact-ledger validation or rendering exists.

- [ ] **Step 3: Integrate validation and rendering**

Update validate_final_report_context and render_final_report; absent ledger renders unknown. Accept closed only when every discovered candidate has a disposition and discovery reports no omissions or reached/uninspected limits; always display configured scan caps to retain the bounded scope.

- [ ] **Step 4: Run focused and related report/orchestration tests**

Run: uv run pytest tests/test_report.py tests/test_orchestration.py -q

Expected: new impact states pass and existing report/coverage behavior remains green.
