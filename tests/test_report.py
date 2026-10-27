import hashlib
import json
from datetime import UTC, datetime

import pytest

import qa_orchestrator.evidence as evidence_module
import qa_orchestrator.report as report_module
from qa_orchestrator.cli import main as cli_main
from qa_orchestrator.contracts import (
    EvidenceRecord,
    FinalReviewFinding,
    FinalReviewReport,
    ImpactCoverageLedger,
    ProfileAttemptMetrics,
    ProfileChangedPathCoverage,
    ProfileExecutionMetrics,
    ReviewAgent,
    ReviewCandidateDecision,
    ReviewContext,
    ReviewCoverageLedger,
    ReviewCriterionCoverage,
    ReviewDiffHunk,
    ReviewFindingLocation,
    ReviewFindingSource,
    ReviewInputConflict,
    ReviewInputManifest,
    ReviewInputSource,
    ReviewNote,
    ReviewScenarioDisposition,
    ReviewVerificationRecord,
    impact_candidate_set_sha256,
    review_changed_paths_sha256,
    review_diff_hunk_inventory_sha256,
)
from qa_orchestrator.refutation import make_refutation_check
from qa_orchestrator.report import (
    impact_coverage_status,
    render_final_report,
    validate_final_report_context,
)


def test_review_boundaries_show_partial_and_reported_gaps(bound_review_context):
    report = FinalReviewReport(
        summary="Partial review.",
        profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
        coverage_gaps=(ReviewNote(text="Diff unavailable", evidence_refs=()),),
        unverified=(ReviewNote(text="CI not checked", evidence_refs=()),),
    )
    rendered = render_final_report(report, context=bound_review_context, outcome="partial")
    assert "## Review boundaries" in rendered
    assert "**Outcome:** partial" in rendered
    assert "code_reviewer" in rendered
    assert "Diff unavailable" in rendered
    assert "CI not checked" in rendered
    assert "**Known completion gaps (host-selected outcome):**" in rendered


def test_review_boundaries_do_not_invent_outcome_or_sources():
    report = FinalReviewReport(summary="Legacy report", profiles_reviewed=(ReviewAgent.CODE_REVIEWER,))
    rendered = render_final_report(report)
    assert "**Outcome:** not provided" in rendered
    assert "unknown; manifest unavailable" in rendered


def test_review_boundaries_show_source_and_unverified_receipt(bound_review_context):
    report = FinalReviewReport(
        summary="Partial review", profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
        coverage_ledger=ReviewCoverageLedger(
            changed_paths_sha256=review_changed_paths_sha256(bound_review_context.changed_files),
            changed_path_count=len(bound_review_context.changed_files),
            profiles=(ProfileChangedPathCoverage(profile=ReviewAgent.CODE_REVIEWER, context_selection_recorded=False),),
        ),
    )
    rendered = render_final_report(report, context=bound_review_context, outcome="partial")
    assert "MR-123" in rendered
    assert "executor input receipt unverified: code\\_reviewer" in rendered


def test_review_boundaries_show_incomplete_diff_source(bound_review_context):
    manifest = bound_review_context.input_manifest
    failed = manifest.sources[0].model_copy(update={
        "source_id": "DIFF", "retrieval_status": "failed", "extraction_status": "failed",
    })
    context = bound_review_context.model_copy(update={
        "input_manifest": manifest.model_copy(update={"sources": (failed,)})
    })
    report = FinalReviewReport(
        summary="Partial", profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
    )
    rendered = render_final_report(report, context=context, outcome="partial")
    assert "**Sources retrieved:** none recorded" in rendered
    assert "**Incomplete sources:** `\"DIFF\"`" in rendered


def _finding(candidate_id: str, *, severity: str, title: str, change_origin: str = "unknown"):
    return FinalReviewFinding(
        candidate_id=candidate_id,
        title=title,
        description="Input can produce the wrong output.",
        confidence="high",
        change_origin=change_origin,
        verification_gap="Runtime verification was not performed.",
        evidence_refs=("E1",),
        source_locations=(ReviewFindingLocation(path="src/api.py", start_line=12),),
        affected_area="API response",
        impact="The caller receives an incorrect result.",
        failure_condition="When input is empty, the changed path returns the wrong value.",
        flip_condition="The bound caller contract proves that value is expected.",
        expected_behavior="The API returns the contractually specified empty-input result.",
        actual_behavior="The changed path returns a different result for empty input.",
        next_step="Add a regression test for the boundary input.",
        severity=severity,
        source_profiles=(ReviewAgent.CODE_REVIEWER,),
    )


def _provider_verified_record(context):
    scope = "pytest tests/test_service.py"
    scope_sha256 = hashlib.sha256(scope.encode("utf-8")).hexdigest()
    started_at = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    completed_at = datetime(2026, 10, 20, 12, 5, tzinfo=UTC)
    return ReviewVerificationRecord(
        verification_id="V-02",
        check_type="ci",
        scenario="focused unit tests",
        status="passed",
        head_sha=context.head_sha,
        environment="Linux, Python 3.13",
        source="ci_provider",
        source_ref="workflow 123 / job 457",
        scope=scope,
        scope_sha256=scope_sha256,
        run_id="github-actions:123:457",
        started_at=started_at,
        completed_at=completed_at,
        provenance="provider_verified",
        receipt={
            "verifier_id": "github-actions",
            "run_id": "github-actions:123:457",
            "head_sha": context.head_sha,
            "conclusion": "passed",
            "scope_sha256": scope_sha256,
            "source_response_sha256": "5" * 64,
            "verified_at": completed_at,
        },
    )


def _impact_ledger(**overrides):
    payload = {
        "head_sha": "1" * 40,
        "changed_paths_sha256": "2" * 64,
        "discovery_sha256": "3" * 64,
        "discovery_method": "pinned_git_symbol_scan",
        "discovery_version": 1,
        "discovery_state": "ready",
        "limits": [
            {
                "name": "candidate_limit",
                "value": 50,
                "reached": False,
                "uninspected": False,
            }
        ],
        "candidates": [
            {
                "candidate_id": "src.module:caller",
                "relation": "caller",
                "locator": "src/module.py",
                "disposition": "reviewed",
                "rationale": "The caller was checked against the changed contract.",
                "evidence_refs": ["E1"],
            }
        ],
        "omissions": [],
    }
    return ImpactCoverageLedger.model_validate({**payload, **overrides})


def _context_with_discovery(
    bound_review_context,
    discovery_sha256="4" * 64,
    *,
    candidates=(("src.module:caller", "caller", "src/module.py"),),
):
    return ReviewContext.model_validate(
        {
            **bound_review_context.model_dump(),
            "neighbor_discovery_sha256": discovery_sha256,
            "neighbor_discovery_candidate_count": len(candidates),
            "neighbor_discovery_candidate_set_sha256": impact_candidate_set_sha256(
                candidates
            ),
        }
    )


def _impact_ledger_for_context(context, **overrides):
    payload = _impact_ledger().model_dump(mode="json")
    payload.update(
        {
            "head_sha": context.head_sha,
            "changed_paths_sha256": review_changed_paths_sha256(context.changed_files),
            "discovery_sha256": context.neighbor_discovery_sha256,
        }
    )
    payload.update(overrides)
    return ImpactCoverageLedger.model_validate(payload)


def _impact_candidate(identity):
    candidate_id, relation, locator = identity
    return {
        "candidate_id": candidate_id,
        "relation": relation,
        "locator": locator,
        "disposition": "reviewed",
        "rationale": "The discovered relationship was checked against the change.",
        "evidence_refs": ["E1"],
    }


def test_impact_coverage_status_is_scoped_and_conservative():
    assert impact_coverage_status(None) == "unknown"
    assert impact_coverage_status(_impact_ledger()) == "closed"
    assert impact_coverage_status(_impact_ledger(discovery_state="partial")) == "partial"
    assert impact_coverage_status(
        _impact_ledger(
            candidates=[
                {
                    "candidate_id": "src.module:caller",
                    "relation": "caller",
                    "locator": "src/module.py",
                    "disposition": "unresolved",
                    "rationale": "The relationship could not be verified.",
                }
            ]
        )
    ) == "partial"
    assert impact_coverage_status(
        _impact_ledger(
            omissions=[{"kind": "candidate_path_limit", "count": 1, "limit": 50}]
        )
    ) == "partial"
    assert impact_coverage_status(
        _impact_ledger(
            limits=[
                {
                    "name": "candidate_limit",
                    "value": 50,
                    "reached": True,
                    "uninspected": True,
                }
            ]
        )
    ) == "partial"


@pytest.mark.parametrize(
    "ledger_changes",
    [
        {"head_sha": "5" * 40},
        {"changed_paths_sha256": "5" * 64},
        {"discovery_sha256": "5" * 64},
    ],
)
def test_impact_ledger_rejects_wrong_head_or_discovery_digest(
    bound_review_context, ledger_changes
):
    context = _context_with_discovery(bound_review_context)
    ledger = _impact_ledger_for_context(context, **ledger_changes)
    report = FinalReviewReport(
        summary="Reviewed impact candidates.",
        profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
        impact_coverage=ledger,
    )

    with pytest.raises(ValueError, match="impact coverage"):
        validate_final_report_context(report, context)


def test_impact_ledger_rejects_evidence_refs_outside_bound_context(bound_review_context):
    context = _context_with_discovery(bound_review_context)
    ledger = _impact_ledger_for_context(
        context,
        candidates=[
            {
                "candidate_id": "src.module:caller",
                "relation": "caller",
                "locator": "src/module.py",
                "disposition": "reviewed",
                "rationale": "The caller was checked against the changed contract.",
                "evidence_refs": ["E99"],
            }
        ],
    )
    report = FinalReviewReport(
        summary="Reviewed impact candidates.",
        profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
        impact_coverage=ledger,
    )

    with pytest.raises(ValueError, match="impact coverage"):
        validate_final_report_context(report, context)


@pytest.mark.parametrize(
    "ledger_candidates",
    [
        (("N0001", "discovery_candidate", "src/one.py"),),
        (
            ("N0001", "discovery_candidate", "src/one.py"),
            ("N0002", "discovery_candidate", "src/two.py"),
            ("N0003", "discovery_candidate", "src/extra.py"),
        ),
        (
            ("N0001", "discovery_candidate", "src/changed-locator.py"),
            ("N0002", "discovery_candidate", "src/two.py"),
        ),
    ],
    ids=("missing", "extra", "identity-changed"),
)
def test_impact_ledger_requires_exact_discovery_candidate_set(
    bound_review_context, ledger_candidates
):
    expected = (
        ("N0001", "discovery_candidate", "src/one.py"),
        ("N0002", "discovery_candidate", "src/two.py"),
    )
    context = _context_with_discovery(bound_review_context, candidates=expected)
    ledger = _impact_ledger_for_context(
        context,
        candidates=[_impact_candidate(identity) for identity in ledger_candidates],
    )
    report = FinalReviewReport(
        summary="Reviewed impact candidates.",
        profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
        impact_coverage=ledger,
    )

    with pytest.raises(ValueError, match="exactly match discovery"):
        validate_final_report_context(report, context)


def test_impact_ledger_closes_only_after_exact_discovery_set_is_disposed(
    bound_review_context,
):
    expected = (
        ("N0001", "discovery_candidate", "src/one.py"),
        ("N0002", "discovery_candidate", "src/two.py"),
    )
    context = _context_with_discovery(bound_review_context, candidates=expected)
    ledger = _impact_ledger_for_context(
        context,
        candidates=[_impact_candidate(identity) for identity in expected],
    )
    report = FinalReviewReport(
        summary="Reviewed every discovered impact candidate.",
        profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
        impact_coverage=ledger,
    )

    validate_final_report_context(report, context)

    assert impact_coverage_status(ledger) == "closed"


def test_report_never_calls_capped_discovery_closed(bound_review_context):
    context = _context_with_discovery(bound_review_context)
    ledger = _impact_ledger_for_context(
        context,
        discovery_state="partial",
        limits=[
            {
                "name": "candidate_limit",
                "value": 50,
                "reached": True,
                "uninspected": True,
            }
        ],
        omissions=[
            {
                "kind": "candidate_path_limit",
                "count": 1,
                "limit": 50,
                "examples": ["src/capped.py"],
            }
        ],
    )
    report = FinalReviewReport(
        summary="Reviewed impact candidates.",
        profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
        impact_coverage=ledger,
    )

    rendered = render_final_report(report, context=context)

    assert "Impact closure: partial" in rendered
    assert "capped" in rendered
    assert "candidate\\_limit = 50" in rendered
    assert "1 omission" in rendered


def test_report_marks_unreturned_discovery_candidates_as_incomplete(
    bound_review_context,
):
    context = _context_with_discovery(bound_review_context).model_copy(
        update={"neighbor_discovery_candidate_results_omitted_by_limit": 17}
    )
    ledger = _impact_ledger_for_context(
        context,
        discovery_state="partial",
        limits=[
            {
                "name": "candidate_result_limit",
                "value": 50,
                "reached": True,
                "uninspected": True,
            }
        ],
        omissions=[
            {
                "kind": "candidate_result_limit",
                "count": 17,
                "limit": 50,
                "examples": ["src/omitted.py"],
            }
        ],
    )
    report = FinalReviewReport(
        summary="Every returned impact candidate was reviewed.",
        profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
        impact_coverage=ledger,
    )

    rendered = render_final_report(report, context=context)
    gaps = evidence_module.review_completion_gaps(context, report)

    assert "Neighbor discovery omitted 17 eligible candidates" in rendered
    assert "blast-radius coverage is incomplete" in rendered
    assert "Impact closure: partial" in rendered
    assert any(
        gap.startswith("neighbor discovery result limit omitted 17 eligible candidates")
        for gap in gaps
    )

    mismatched_report = report.model_copy(
        update={"impact_coverage": _impact_ledger_for_context(context)}
    )
    with pytest.raises(
        ValueError,
        match="impact coverage omissions do not match the bound candidate result limit",
    ):
        validate_final_report_context(mismatched_report, context)


def test_impact_rationale_is_markdown_escaped(bound_review_context):
    context = _context_with_discovery(bound_review_context)
    ledger = _impact_ledger_for_context(
        context,
        candidates=[
            {
                "candidate_id": "src.module:caller",
                "relation": "caller",
                "locator": "src/module.py",
                "disposition": "unresolved",
                "rationale": "<script>alert(1)</script>",
                "evidence_refs": ["E1"],
            }
        ],
    )
    report = FinalReviewReport(
        summary="Reviewed impact candidates.",
        profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
        impact_coverage=ledger,
    )

    rendered = render_final_report(report, context=context)

    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered


def test_impact_locator_is_markdown_escaped(bound_review_context):
    candidate_identity = (
        "src.module:caller",
        "caller",
        "<script>alert(1)</script>.py",
    )
    context = _context_with_discovery(
        bound_review_context, candidates=(candidate_identity,)
    )
    ledger = _impact_ledger_for_context(
        context,
        candidates=[
            {
                "candidate_id": "src.module:caller",
                "relation": "caller",
                "locator": "<script>alert(1)</script>.py",
                "disposition": "reviewed",
                "rationale": "The neighbor was checked against the contract.",
                "evidence_refs": ["E1"],
            }
        ],
    )
    report = FinalReviewReport(
        summary="Reviewed impact candidates.",
        profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
        impact_coverage=ledger,
    )

    rendered = render_final_report(report, context=context)

    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered


def test_report_marks_missing_impact_ledger_unknown():
    report = FinalReviewReport(
        summary="Reviewed impact candidates.",
        profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
    )

    rendered = render_final_report(report)

    assert "Impact closure: unknown" in rendered


def test_renderer_orders_findings_and_includes_traceability():
    report = FinalReviewReport(
        summary="Reviewed the API change.",
        profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
        finding_candidates=(
            _finding("F-02", severity="high", title="High issue"),
            _finding(
                "F-01", severity="blocker", title="Unsafe [flow]", change_origin="pre_existing"
            ),
        ),
        coverage_gaps=(ReviewNote(text="Stage was unavailable.", evidence_refs=("E2",)),),
        unverified=(ReviewNote(text="Release behavior is unverified."),),
    )

    rendered = render_final_report(report)

    assert rendered.index("Blocker") < rendered.index("High issue")
    assert "Unsafe \\[flow\\]" in rendered
    assert "Change origin:** существовал до изменения" in rendered
    assert 'Source locations:** `"src/api.py:12"`' in rendered
    assert "Next step:** Add a regression test" in rendered
    assert "Failure condition:** When input is empty" in rendered
    assert "Expected behavior:** The API returns the contractually specified empty\\-input result\\." in rendered
    assert "Source-predicted actual behavior:** The changed path returns a different result for empty input\\." in rendered
    assert "Counterevidence that would change the conclusion" in rendered
    assert "## Coverage gaps" in rendered
    assert "## Unverified" in rendered


def test_report_labels_pr_exacerbated_findings():
    report = FinalReviewReport(
        summary="Reviewed the API change.",
        profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
        finding_candidates=(
            _finding(
                "F-01",
                severity="medium",
                title="Existing issue worsened by this PR",
                change_origin="exacerbated_by_pr",
            ),
        ),
    )

    rendered = render_final_report(report)

    assert "Change origin:** существовал до PR; PR усилил дефект" in rendered


def test_report_cli_writes_markdown(tmp_path):
    source = tmp_path / "report.json"
    output = tmp_path / "report.md"
    report = FinalReviewReport(
        summary="No confirmed finding.", profiles_reviewed=(ReviewAgent.CODE_REVIEWER,)
    )
    source.write_text(report.model_dump_json(), encoding="utf-8")

    assert (
        cli_main(["report", "render", "--input", str(source), "--output", str(output)])
        == 0
    )
    assert output.read_text(encoding="utf-8").startswith("# QA review\n")
    assert json.loads(source.read_text(encoding="utf-8"))["summary"] == "No confirmed finding."


def test_report_cli_rejects_source_locations_outside_bound_evidence(
    tmp_path, capsys, bound_review_context
):
    context = ReviewContext.model_validate(
        {
            **bound_review_context.model_dump(),
            "evidence_registry": (
                EvidenceRecord(
                    evidence_ref="E1",
                    kind="source",
                    revision=bound_review_context.head_sha,
                    path="src/api.py",
                    start_line=10,
                    end_line=20,
                    sha256="4" * 64,
                ),
                EvidenceRecord(
                    evidence_ref="E2",
                    kind="requirement",
                    locator="TASK-123",
                    sha256="5" * 64,
                ),
            ),
        }
    )
    finding = _finding("F-01", severity="high", title="Issue").model_copy(
        update={
            "source_locations": (
                ReviewFindingLocation(path="src/other.py", start_line=12),
            )
        }
    )
    report = FinalReviewReport(
        summary="Reviewed the API change.",
        profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
        finding_candidates=(finding,),
    )
    report_path = tmp_path / "report.json"
    context_path = tmp_path / "context.json"
    report_path.write_text(report.model_dump_json(), encoding="utf-8")
    context_path.write_text(context.model_dump_json(), encoding="utf-8")

    assert (
        cli_main(
            [
                "report",
                "render",
                "--input",
                str(report_path),
                "--context",
                str(context_path),
            ]
        )
        == 2
    )
    error = capsys.readouterr().err
    assert "Ошибка:" in error
    assert "finding location is not supported" in error


def test_renderer_maps_requirements_and_mr_claims_to_code_tests_and_outcomes(
    bound_review_context,
):
    context = ReviewContext.model_validate(
        {
            **bound_review_context.model_dump(),
            "evidence_refs": ("E1", "D1", "R1", "C1", "T1"),
            "evidence_registry": (
                EvidenceRecord(
                    evidence_ref="E1",
                    kind="source",
                    revision=bound_review_context.head_sha,
                    path="src/api.py",
                    start_line=10,
                    end_line=20,
                    sha256="4" * 64,
                ),
                EvidenceRecord(
                    evidence_ref="D1",
                    kind="diff",
                    revision=bound_review_context.head_sha,
                    sha256=bound_review_context.diff_sha256,
                ),
                EvidenceRecord(
                    evidence_ref="R1",
                    kind="requirement",
                    locator="TASK-123#acceptance-1",
                    sha256="5" * 64,
                ),
                EvidenceRecord(
                    evidence_ref="C1",
                    kind="claim",
                    locator="MR!17:description#2",
                    sha256="6" * 64,
                ),
                EvidenceRecord(
                    evidence_ref="T1",
                    kind="source",
                    revision=bound_review_context.head_sha,
                    path="tests/test_api.py",
                    start_line=30,
                    end_line=45,
                    sha256="7" * 64,
                ),
            ),
        }
    )
    report = FinalReviewReport(
        summary="Reviewed the API change.",
        profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
        criterion_coverage=(
            ReviewCriterionCoverage(
                criterion_ref="R1",
                criterion_kind="requirement",
                expected_behavior="A missing account returns an empty result.",
                outcome="confirmed",
                implementation_refs=("E1",),
                test_refs=("T1",),
                rationale="The implementation and focused test agree with the criterion.",
            ),
        ),
    )

    validate_final_report_context(report, context)
    rendered = render_final_report(report, context=context)

    assert "## Requirements and MR claims" in rendered
    assert "A missing account returns an empty result" in rendered
    assert "Implementation evidence:** E1" in rendered
    assert "Test/CI evidence:** T1" in rendered
    assert "MR claim" in rendered
    assert "C1" in rendered
    assert "не проверено" in rendered
    assert "No explicit assessment was submitted" in rendered


def test_criterion_coverage_rejects_unknown_refs_and_wrong_evidence_kinds(
    bound_review_context,
):
    context = ReviewContext.model_validate(
        {
            **bound_review_context.model_dump(),
            "evidence_refs": ("E1", "R1", "T1"),
            "evidence_registry": (
                EvidenceRecord(
                    evidence_ref="E1",
                    kind="source",
                    revision=bound_review_context.head_sha,
                    path="src/api.py",
                    sha256="4" * 64,
                ),
                EvidenceRecord(
                    evidence_ref="R1",
                    kind="requirement",
                    locator="TASK-123#acceptance-1",
                    sha256="5" * 64,
                ),
                EvidenceRecord(
                    evidence_ref="T1",
                    kind="requirement",
                    locator="TASK-123#acceptance-2",
                    sha256="6" * 64,
                ),
            ),
        }
    )

    unknown_criterion = FinalReviewReport(
        summary="Reviewed the API change.",
        profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
        criterion_coverage=(
            ReviewCriterionCoverage(
                criterion_ref="R2",
                criterion_kind="requirement",
                expected_behavior="An account must be returned.",
                outcome="confirmed",
                implementation_refs=("E1",),
                rationale="The source implements the expected result.",
            ),
        ),
    )
    with pytest.raises(ValueError, match="unregistered requirement or MR claim"):
        validate_final_report_context(unknown_criterion, context)

    wrong_support_kind = FinalReviewReport(
        summary="Reviewed the API change.",
        profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
        criterion_coverage=(
            ReviewCriterionCoverage(
                criterion_ref="R1",
                criterion_kind="requirement",
                expected_behavior="An account must be returned.",
                outcome="confirmed",
                implementation_refs=("T1",),
                rationale="The linked evidence is registered as another requirement.",
            ),
        ),
    )
    with pytest.raises(ValueError, match="implementation evidence must reference source or diff"):
        validate_final_report_context(wrong_support_kind, context)


def test_scenario_disposition_requires_evidence_and_nonduplicate_scenarios():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="evidence_refs"):
        ReviewScenarioDisposition(
            scenario="A retry repeats the side effect.",
            disposition="unverified",
            evidence_refs=(),
            rationale="The retry path is not included in the packet.",
        )

    scenario = ReviewScenarioDisposition(
        scenario="A retry repeats the side effect.",
        disposition="unverified",
        evidence_refs=("E1",),
        rationale="The retry path is not included in the packet.",
    )
    with pytest.raises(ValidationError, match="scenario coverage scenarios must be unique"):
        FinalReviewReport(
            summary="Reviewed the retry behavior.",
            profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
            scenario_coverage=(
                scenario,
                scenario.model_copy(update={"scenario": "  A RETRY repeats the side effect. "}),
            ),
        )


def test_final_report_validates_scenario_refs_and_renders_the_matrix(bound_review_context):
    valid = ReviewScenarioDisposition(
        scenario="A retry can repeat the write.",
        disposition="unverified",
        evidence_refs=("E2",),
        rationale="The packet does not show the downstream idempotency behavior.",
    )
    report = FinalReviewReport(
        summary="Reviewed the retry behavior.",
        profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
        scenario_coverage=(valid,),
    )

    validate_final_report_context(report, bound_review_context)
    rendered = render_final_report(report, context=bound_review_context)
    assert "## Scenario disposition matrix" in rendered
    assert "A retry can repeat the write" in rendered
    assert "unverified" in rendered
    assert "E2" in rendered

    unknown_ref = report.model_copy(
        update={
            "scenario_coverage": (
                valid.model_copy(update={"evidence_refs": ("E9",)}),
            )
        }
    )
    with pytest.raises(ValueError, match="report cites refs outside the bound context"):
        validate_final_report_context(unknown_ref, bound_review_context)


def test_renderer_resolves_refs_from_bound_context(bound_review_context):
    context = ReviewContext.model_validate(
        {
            **bound_review_context.model_dump(),
            "evidence_registry": (
                EvidenceRecord(
                    evidence_ref="E1",
                    kind="source",
                    revision=bound_review_context.head_sha,
                    path="src/api.py",
                    start_line=10,
                    end_line=20,
                    sha256="4" * 64,
                ),
                EvidenceRecord(
                    evidence_ref="E2",
                    kind="requirement",
                    locator="TASK-123",
                    sha256="5" * 64,
                ),
            ),
        }
    )
    report = FinalReviewReport(
        summary="Reviewed the API change.",
        profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
        finding_candidates=(_finding("F-01", severity="high", title="Issue"),),
    )

    rendered = render_final_report(report, context=context)

    assert 'Evidence refs:** E1 — `"src/api.py:10-20@111111111111' in rendered


def test_renderer_maps_host_coverage_ledger_to_bound_changed_paths(bound_review_context):
    context = ReviewContext.model_validate(
        {
            **bound_review_context.model_dump(),
            "changed_files": ("src/service.py", "src/binary.dat", "docs/README.md"),
            "evidence_registry": (
                EvidenceRecord(
                    evidence_ref="E1",
                    kind="source",
                    revision=bound_review_context.head_sha,
                    path="src/service.py",
                    sha256="4" * 64,
                    source_text_available=True,
                ),
                EvidenceRecord(
                    evidence_ref="E2",
                    kind="source",
                    revision=bound_review_context.head_sha,
                    path="src/binary.dat",
                    sha256="5" * 64,
                    source_text_available=False,
                ),
            ),
        }
    )
    report = FinalReviewReport(
        summary="Reviewed the changed service path.",
        profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
        coverage_ledger=ReviewCoverageLedger(
            changed_paths_sha256=review_changed_paths_sha256(context.changed_files),
            changed_path_count=3,
            registered_head_source_path_indexes=(0, 1),
            head_source_excerpt_path_indexes=(0,),
            head_source_text_unavailable_path_indexes=(1,),
            profiles=(
                ProfileChangedPathCoverage(
                    profile=ReviewAgent.CODE_REVIEWER,
                    context_selection_recorded=True,
                    executor_input_receipt_verified=True,
                    source_excerpt_included_path_indexes=(0,),
                    reviewer_attested_path_indexes=(0,),
                    execution_metrics=ProfileExecutionMetrics(
                        started_at=datetime(2026, 10, 19, 12, tzinfo=UTC),
                        completed_at=datetime(2026, 10, 19, 12, 0, 3, tzinfo=UTC),
                        input_tokens=1200,
                        output_tokens=240,
                        attempt_count=1,
                        attempts=(
                            ProfileAttemptMetrics(
                                profile=ReviewAgent.CODE_REVIEWER,
                                attempt=1,
                                outcome="completed",
                                review_head_sha=context.head_sha,
                                review_diff_sha256=context.diff_sha256,
                                started_at=datetime(2026, 10, 19, 12, tzinfo=UTC),
                                completed_at=datetime(2026, 10, 19, 12, 0, 3, tzinfo=UTC),
                                queue_seconds=0.2,
                                executor_seconds=2.8,
                                preparation_seconds=0.4,
                                model_seconds=2.0,
                                validation_seconds=0.3,
                                input_tokens=1200,
                                output_tokens=240,
                                submitted_prompt_sha256="8" * 64,
                                prompt_write_status="written",
                                source_packet_sha256=context.evidence_packet_sha256,
                                selected_packet_sha256="9" * 64,
                                included_evidence_refs=("E1",),
                                included_source_paths=("src/service.py",),
                            ),
                        ),
                    ),
                ),
            ),
            diff_hunk_inventory_sha256=context.diff_hunk_inventory_sha256,
            diff_hunk_count=len(context.diff_hunks),
            unreviewed_diff_hunk_indexes=(),
        ),
    )

    rendered = render_final_report(report, context=context)

    assert "## Changed-path coverage" in rendered
    assert "Paths with registered head-source evidence: 2/3; paths with verified text excerpts: 1/3." in rendered
    assert 'No registered head-source record: `"docs/README.md"`' in rendered
    assert 'Source text explicitly marked unavailable: `"src/binary.dat"`' in rendered
    assert 'Not explicitly attested as reviewed: `"src/binary.dat"`' in rendered
    assert 'Executor-attested as reviewed: `"src/service.py"`' in rendered
    assert "executor input receipt verified" in rendered
    assert 'Paths with included text excerpts: `"src/service.py"`' in rendered
    assert "## Profile execution" in rendered
    assert "elapsed 3.00s; attempts 1; input tokens 1200; output tokens 240" in rendered
    assert "head 111111111111" in rendered
    assert "CLI stdin delivery write completed (host observed)" in rendered
    assert "prompt sha256 888888888888" in rendered
    assert "selected packet sha256 999999999999" in rendered
    assert "included refs E1" in rendered
    assert 'source paths `"src/service.py"`' in rendered
    assert "queue 0.20s; executor 2.80s; head" in rendered
    assert "does not prove provider receipt or model reasoning" in rendered
    validate_final_report_context(report, context)
    with pytest.raises(ValueError, match="coverage ledger does not match"):
        validate_final_report_context(report, bound_review_context)


@pytest.mark.parametrize(
    ("reviewed_indexes", "expected_coverage", "expected_unreviewed"),
    (((), "0/1", "0"), ((0,), "1/1", None)),
)
def test_report_accounts_for_bound_diff_hunk_attestations(
    bound_review_context, reviewed_indexes, expected_coverage, expected_unreviewed
):
    hunk = ReviewDiffHunk(
        index=0,
        changed_path_index=0,
        old_start=10,
        old_count=1,
        new_start=10,
        new_count=1,
        header="@@ -10 +10 @@",
        sha256="a" * 64,
    )
    context = bound_review_context.model_copy(
        update={
            "diff_hunks": (hunk,),
            "diff_hunk_inventory_sha256": review_diff_hunk_inventory_sha256(
                bound_review_context.diff_sha256, (hunk,)
            ),
        }
    )
    ledger = ReviewCoverageLedger(
        changed_paths_sha256=review_changed_paths_sha256(context.changed_files),
        changed_path_count=len(context.changed_files),
        profiles=(
            ProfileChangedPathCoverage(
                profile=ReviewAgent.CODE_REVIEWER,
                context_selection_recorded=True,
                reviewer_attested_path_indexes=(0,),
                reviewed_diff_hunk_indexes=reviewed_indexes,
            ),
        ),
        diff_hunk_inventory_sha256=context.diff_hunk_inventory_sha256,
        diff_hunk_count=1,
        unreviewed_diff_hunk_indexes=() if reviewed_indexes else (0,),
    )
    report = FinalReviewReport(
        summary="Reviewed the changed hunk.",
        profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
        coverage_ledger=ledger,
    )

    rendered = render_final_report(report, context=context)
    gaps = evidence_module.review_completion_gaps(context, report)

    assert f"Diff hunk coverage: {expected_coverage} hunk indexes" in rendered
    if expected_unreviewed is None:
        assert "Unreviewed diff hunk indexes:" not in rendered
        assert not any(gap.startswith("unreviewed diff hunk indexes:") for gap in gaps)
    else:
        assert f"Unreviewed diff hunk indexes: {expected_unreviewed}" in rendered
        assert f"unreviewed diff hunk indexes: {expected_unreviewed}" in gaps


def test_partial_diff_hunk_inventory_blocks_completion_after_indexed_hunks_are_reviewed(
    bound_review_context,
):
    hunk = ReviewDiffHunk(
        index=0,
        changed_path_index=0,
        old_start=10,
        old_count=1,
        new_start=10,
        new_count=1,
        header="@@ -10 +10 @@",
        sha256="a" * 64,
    )
    hunks = (hunk,)
    inventory_digest = review_diff_hunk_inventory_sha256(
        bound_review_context.diff_sha256, hunks
    )
    context = ReviewContext.model_validate(
        {
            **bound_review_context.model_dump(mode="python"),
            "diff_hunks": hunks,
            "diff_hunk_inventory_sha256": inventory_digest,
            "diff_hunk_inventory_status": "partial",
            "diff_hunk_inventory_count_at_least": 501,
        }
    )
    report = FinalReviewReport(
        summary="Every available indexed hunk was reviewed.",
        profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
        coverage_ledger=ReviewCoverageLedger(
            changed_paths_sha256=review_changed_paths_sha256(context.changed_files),
            changed_path_count=len(context.changed_files),
            profiles=(
                ProfileChangedPathCoverage(
                    profile=ReviewAgent.CODE_REVIEWER,
                    context_selection_recorded=True,
                    reviewed_diff_hunk_indexes=(0,),
                ),
            ),
            diff_hunk_inventory_sha256=inventory_digest,
            diff_hunk_count=1,
            unreviewed_diff_hunk_indexes=(),
        ),
    )

    rendered = render_final_report(report, context=context)
    without_ledger = render_final_report(
        FinalReviewReport(
            summary="Report without a coverage ledger.",
            profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
        ),
        context=context,
    )
    gaps = evidence_module.review_completion_gaps(context, report)

    assert "Diff hunk inventory: partial; at least 501 total, 1 indexed." in rendered
    assert "Diff hunk inventory: partial; at least 501 total, 1 indexed." in without_ledger
    assert (
        "diff hunk inventory is partial: at least 501 hunks exist, but only 1 are indexed"
    ) in gaps
    assert not any(gap.startswith("unreviewed diff hunk indexes:") for gap in gaps)


def test_strict_completion_requires_a_bound_diff_hunk_inventory(bound_review_context):
    payload = bound_review_context.model_dump()
    payload.pop("diff_hunks")
    payload.pop("diff_hunk_inventory_sha256")
    context = ReviewContext.model_validate(payload)
    report = FinalReviewReport(
        summary="Review report without a complete diff inventory.",
        profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
        coverage_ledger=ReviewCoverageLedger(
            changed_paths_sha256=review_changed_paths_sha256(context.changed_files),
            changed_path_count=len(context.changed_files),
            profiles=(
                ProfileChangedPathCoverage(
                    profile=ReviewAgent.CODE_REVIEWER,
                    context_selection_recorded=True,
                ),
            ),
        ),
    )

    gaps = evidence_module.review_completion_gaps(context, report)

    assert "complete diff hunk inventory is not bound to the review" in gaps


def test_new_completion_requires_hunk_ledger_but_legacy_restore_can_omit_it(
    bound_review_context,
):
    report = FinalReviewReport(
        summary="Report without a hunk ledger.",
        profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
    )

    strict_gaps = evidence_module.review_completion_gaps(bound_review_context, report)
    legacy_gaps = evidence_module.review_completion_gaps(
        bound_review_context,
        report,
        allow_legacy_missing_hunk_ledger=True,
    )

    assert "diff hunk coverage ledger does not match the bound inventory" in strict_gaps
    assert not any("diff hunk" in gap for gap in legacy_gaps)


def _input_source(
    source_id,
    *,
    kind="jira_issue",
    required=True,
    retrieval_status="complete",
    pagination_status="complete",
    extraction_status="complete",
    criterion_refs=(),
):
    return ReviewInputSource(
        source_id=source_id,
        kind=kind,
        locator=f"jira:{source_id}",
        required=required,
        retrieval_status=retrieval_status,
        pagination_status=pagination_status,
        extraction_status=extraction_status,
        criterion_refs=criterion_refs,
    )


def _context_with_input_sources(bound_review_context, sources):
    return ReviewContext.model_validate(
        {
            **bound_review_context.model_dump(),
            "input_manifest": {"sources": tuple(sources)},
        }
    )


def _context_with_changed_paths(bound_review_context, changed_files, sources):
    return ReviewContext.model_validate(
        {
            **bound_review_context.model_dump(),
            "changed_files": tuple(changed_files),
            "input_manifest": {"sources": tuple(sources)},
        }
    )


def test_report_renders_incomplete_required_input_as_partial(bound_review_context):
    context = _context_with_input_sources(
        bound_review_context,
        (
            _input_source(
                "JIRA-1",
                retrieval_status="complete",
                pagination_status="incomplete",
                extraction_status="partial",
            ),
        ),
    )
    report = FinalReviewReport(
        summary="Reviewed the API change.",
        profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
    )

    rendered = render_final_report(report, context=context)

    assert "Input completeness: partial" in rendered
    assert "JIRA-1" in rendered
    assert "pagination incomplete" in rendered
    assert "extraction partial" in rendered


def test_manifest_v2_unknown_inventory_and_conflict_assessment_block_completion(
    bound_review_context,
):
    context = _context_with_input_sources(
        bound_review_context,
        (_input_source("MR-1"),),
    )
    manifest = ReviewInputManifest(
        schema_version=2,
        sources=context.input_manifest.sources,
        source_inventory_status="unknown",
        source_conflict_status="unknown",
    )
    context = context.model_copy(update={"input_manifest": manifest})

    summary = evidence_module.input_manifest_completeness(context)
    rendered = render_final_report(
        FinalReviewReport(
            summary="The source inventory has not been reconciled.",
            profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
        ),
        context=context,
    )

    assert summary["status"] == "unknown"
    assert "source inventory is unknown" in summary["status_reason"]
    assert "description conflict assessment is unknown" in summary["status_reason"]
    assert "Source inventory assessment:** unknown" in rendered
    assert "Description conflict assessment:** unknown" in rendered
    assert evidence_module.review_completion_gaps(
        context,
        FinalReviewReport(
            summary="Incomplete source inventory.",
            profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
        ),
    )


def test_unresolved_description_conflict_is_rendered_and_blocks_completion(
    bound_review_context,
):
    context = _context_with_input_sources(
        bound_review_context,
        (_input_source("JIRA-1"), _input_source("MR-1", kind="merge_request")),
    )
    conflict = ReviewInputConflict(
        conflict_id="CONFLICT-1",
        source_ids=("JIRA-1", "MR-1"),
        disposition="unresolved",
        summary="The descriptions specify different retry behavior.",
    )
    manifest = ReviewInputManifest(
        schema_version=2,
        sources=context.input_manifest.sources,
        source_inventory_status="complete",
        source_conflict_status="unresolved",
        source_conflicts=(conflict,),
    )
    context = context.model_copy(update={"input_manifest": manifest})
    report = FinalReviewReport(
        summary="A material source conflict remains.",
        profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
    )

    summary = evidence_module.input_manifest_completeness(context)
    rendered = render_final_report(report, context=context)

    assert summary["status"] == "partial"
    assert "CONFLICT-1" in summary["status_reason"]
    assert "CONFLICT-1" in rendered
    assert "unresolved" in rendered
    assert any("input manifest is partial" in gap for gap in evidence_module.review_completion_gaps(context, report))


def test_api_scope_requires_contract_and_consumer_map(bound_review_context):
    context = _context_with_changed_paths(
        bound_review_context,
        ("src/api/checkout.py",),
        (_input_source("MR-1"),),
    )

    summary = evidence_module.input_manifest_completeness(context)

    assert summary["scope_policy_version"] == 1
    assert summary["scope_categories"] == ["api"]
    assert summary["scope_required_source_kinds"] == [
        "api_consumer_map",
        "api_contract",
    ]
    assert summary["missing_scope_source_kinds"] == [
        "api_consumer_map",
        "api_contract",
    ]
    assert summary["status"] == "partial"
    gaps = evidence_module.review_completion_gaps(
        context,
        FinalReviewReport(
            summary="Scope sources are missing.",
            profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
        ),
    )
    assert any("required scope source kinds missing from manifest" in gap for gap in gaps)


def test_missing_manifest_is_partial_when_changed_paths_define_requirements(
    bound_review_context,
):
    context = ReviewContext.model_validate(
        {
            **bound_review_context.model_dump(),
            "changed_files": ("src/api/checkout.py",),
            "input_manifest": None,
        }
    )

    summary = evidence_module.input_manifest_completeness(context)

    assert summary["status"] == "partial"
    assert summary["missing_scope_source_kinds"] == [
        "api_consumer_map",
        "api_contract",
    ]


def test_scope_requirements_need_required_complete_sources(bound_review_context):
    context = _context_with_changed_paths(
        bound_review_context,
        ("src/api/checkout.py",),
        (
            _input_source("MR-1"),
            _input_source("API-CONTRACT", kind="api_contract", required=False),
            _input_source(
                "API-CONSUMERS",
                kind="api_consumer_map",
                retrieval_status="failed",
            ),
        ),
    )

    summary = evidence_module.input_manifest_completeness(context)

    assert summary["missing_scope_source_kinds"] == ["api_contract"]
    assert summary["status"] == "partial"
    assert summary["source_counts"]["incomplete_required"] == 1


def test_complete_api_scope_requires_both_boundaries(bound_review_context):
    context = _context_with_changed_paths(
        bound_review_context,
        ("src/api/checkout.py",),
        (
            _input_source("MR-1"),
            _input_source("API-CONTRACT", kind="api_contract"),
            _input_source("API-CONSUMERS", kind="api_consumer_map"),
        ),
    )

    summary = evidence_module.input_manifest_completeness(context)

    assert summary["missing_scope_source_kinds"] == []
    assert summary["status"] == "complete"


def test_scope_policy_unions_database_event_deployment_and_service_requirements(
    bound_review_context,
):
    context = _context_with_changed_paths(
        bound_review_context,
        (
            "db/migrations/001_add_status.sql",
            "events/deposit_published.py",
            "deploy/values.yaml",
            "services/wallet/client.py",
        ),
        (_input_source("MR-1"),),
    )

    summary = evidence_module.input_manifest_completeness(context)

    assert summary["scope_categories"] == [
        "database",
        "deployment",
        "event",
        "service_boundary",
    ]
    assert summary["scope_required_source_kinds"] == [
        "database_consumer_map",
        "database_schema",
        "deployment_manifest",
        "event_consumer_map",
        "event_contract",
        "service_dependency_map",
    ]
    assert summary["status"] == "partial"


def test_scope_policy_requires_config_authorization_and_job_sources(bound_review_context):
    context = _context_with_changed_paths(
        bound_review_context,
        (
            "config/runtime.yaml",
            "auth/roles.py",
            "jobs/cleanup.py",
        ),
        (_input_source("MR-1"),),
    )

    summary = evidence_module.input_manifest_completeness(context)

    assert summary["scope_categories"] == ["authorization", "config", "job"]
    assert summary["scope_required_source_kinds"] == [
        "authorization_consumer_map",
        "authorization_policy",
        "configuration_contract",
        "flag_consumer_map",
        "job_registration",
        "trigger_consumer_map",
    ]


def test_input_report_lists_scope_policy_gaps(bound_review_context):
    context = _context_with_changed_paths(
        bound_review_context,
        ("src/api/checkout.py",),
        (_input_source("MR-1"),),
    )
    report = FinalReviewReport(
        summary="Reviewed the API change.",
        profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
    )

    rendered = render_final_report(report, context=context)

    assert '- **Path-derived scope:** policy v1; `"api"`' in rendered
    assert '`"api_consumer_map"`' in rendered
    assert '`"api_contract"`' in rendered


def test_report_marks_missing_input_manifest_unknown(bound_review_context):
    report = FinalReviewReport(
        summary="Reviewed the API change.",
        profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
    )
    legacy_context = ReviewContext.model_validate(
        {**bound_review_context.model_dump(), "input_manifest": None}
    )

    rendered = render_final_report(report, context=legacy_context)

    assert "Input completeness: unknown" in rendered


def test_report_without_context_marks_input_completeness_unknown():
    report = FinalReviewReport(
        summary="Reviewed the API change.",
        profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
    )

    rendered = render_final_report(report)

    assert "Input completeness: unknown" in rendered


def test_report_lists_manifest_criterion_missing_from_registry(bound_review_context):
    context = _context_with_input_sources(
        bound_review_context,
        (_input_source("JIRA-1", criterion_refs=("R-2",)),),
    )

    summary = evidence_module.input_manifest_completeness(context)

    assert summary["missing_criterion_refs"] == ["R-2"]
    assert summary["status"] == "partial"


def test_manifest_without_registered_requirement_is_partial(bound_review_context):
    context_with_requirement = ReviewContext.model_validate(
        {
            **bound_review_context.model_dump(),
            "evidence_refs": ("R1",),
            "evidence_registry": (
                EvidenceRecord(
                    evidence_ref="R1",
                    kind="requirement",
                    locator="TASK-123#acceptance-1",
                    sha256="5" * 64,
                ),
            ),
        }
    )
    context = _context_with_input_sources(
        context_with_requirement,
        (_input_source("JIRA-1", criterion_refs=()),),
    )

    summary = evidence_module.input_manifest_completeness(context)
    rendered = render_final_report(
        FinalReviewReport(
            summary="Reviewed the API change.",
            profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
        ),
        context=context,
    )

    assert summary["status"] == "partial"
    assert summary["unlisted_criterion_refs"] == ["R1"]
    assert "**Unlisted registered criterion refs:** R1" in rendered


def test_empty_criteria_can_be_complete_after_complete_extraction(bound_review_context):
    context = _context_with_input_sources(
        bound_review_context,
        (_input_source("JIRA-1", criterion_refs=()),),
    )

    summary = evidence_module.input_manifest_completeness(context)

    assert summary["status"] == "complete"


def test_incomplete_optional_source_is_listed_without_downgrading_required_status(
    bound_review_context,
):
    context = _context_with_input_sources(
        bound_review_context,
        (
            _input_source("JIRA-1"),
            _input_source(
                "OPTIONAL-1",
                required=False,
                retrieval_status="failed",
                pagination_status="unknown",
                extraction_status="unknown",
            ),
        ),
    )

    summary = evidence_module.input_manifest_completeness(context)

    assert summary["status"] == "complete"
    assert [source["source_id"] for source in summary["incomplete_sources"]] == [
        "OPTIONAL-1"
    ]


def test_manifest_without_required_sources_is_unknown(bound_review_context):
    context = _context_with_input_sources(
        bound_review_context,
        (_input_source("OPTIONAL-1", required=False),),
    )

    summary = evidence_module.input_manifest_completeness(context)

    assert summary["status"] == "unknown"


def test_verification_records_are_bound_to_exact_head_and_rendered(bound_review_context):
    context = ReviewContext.model_validate(
        {
            **bound_review_context.model_dump(),
            "evidence_refs": ("R1",),
            "evidence_registry": (
                EvidenceRecord(
                    evidence_ref="R1",
                    kind="runtime",
                    locator="ci://workflow/123",
                    revision=bound_review_context.head_sha,
                    sha256="4" * 64,
                ),
            ),
        }
    )
    record = ReviewVerificationRecord(
        verification_id="V-01",
        check_type="ci",
        scenario="unit and integration test suites",
        status="passed",
        head_sha=context.head_sha,
        environment="Linux, Python 3.13",
        source="ci_provider",
        source_ref="workflow 123 / job 456",
        run_url="https://github.com/example/repo/actions/runs/123",
        scope="pytest tests/test_service.py",
        evidence_refs=("R1",),
    )
    provider_record = _provider_verified_record(context)
    report = FinalReviewReport(
        summary="Reviewed and checked the change.",
        profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
        verification_records=(record, provider_record),
    )

    validate_final_report_context(report, context)
    rendered = render_final_report(report, context=context)

    assert "## Tests and CI verification" in rendered
    assert "host-supplied records bound to a head SHA" in rendered
    assert "unit and integration test suites" in rendered
    assert "workflow 123 / job 456" in rendered
    assert "<https://github.com/example/repo/actions/runs/123>" in rendered
    assert "status: passed; head binding: current" in rendered
    assert "provenance: host reported, unverified" in rendered
    assert "provenance: provider verified" in rendered
    assert "run ID:" in rendered
    assert "scope sha256:" in rendered
    assert "verified at:" in rendered
    assert "provider response sha256:" in rendered
    assert "scope: pytest tests/" in rendered
    assert report_module.verification_binding(record, context) == "current"

    wrong_head = report.model_copy(
        update={
            "verification_records": (
                record,
                provider_record,
                record.model_copy(update={"head_sha": "0" * 40, "evidence_refs": ()}),
            )
        }
    )
    validate_final_report_context(wrong_head, context)
    stale_rendered = render_final_report(wrong_head, context=context)
    assert "status: passed, stale, not a current pass" in stale_rendered
    assert report_module.verification_binding(
        wrong_head.verification_records[-1], context
    ) == "stale"


def test_verification_evidence_requires_head_bound_runtime_record(bound_review_context):
    older_sha = "0" * 40
    context = ReviewContext.model_validate(
        {
            **bound_review_context.model_dump(),
            "evidence_refs": ("E1",),
            "evidence_registry": (
                EvidenceRecord(
                    evidence_ref="E1",
                    kind="runtime",
                    revision=older_sha,
                    locator="runtime://stage/session/123",
                    sha256="4" * 64,
                ),
            ),
        }
    )
    report = FinalReviewReport(
        summary="Checked the change.",
        profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
        verification_records=(
            ReviewVerificationRecord(
                verification_id="V-01",
                check_type="runtime",
                scenario="runtime session check",
                status="passed",
                head_sha=older_sha,
                environment="stage",
                source="runtime_system",
                source_ref="session 123",
                evidence_refs=("E1",),
            ),
        ),
    )

    validate_final_report_context(report, context)
    assert report_module.verification_binding(
        report.verification_records[0], context
    ) == "stale"
    rendered = render_final_report(report, context=context)
    assert "status: passed, stale, not a current pass" in rendered

    mismatched_context = ReviewContext.model_validate(
        {
            **context.model_dump(),
            "evidence_registry": (
                EvidenceRecord(
                    evidence_ref="E1",
                    kind="runtime",
                    revision=context.head_sha,
                    locator="runtime://stage/session/123",
                    sha256="4" * 64,
                ),
            ),
        }
    )
    with pytest.raises(ValueError, match="runtime evidence must match the verification record head"):
        validate_final_report_context(report, mismatched_context)


@pytest.mark.parametrize("status", ["failed", "skipped", "not_run", "cancelled", "unknown"])
def test_nonpassing_verification_states_preserve_reason_without_pass_badge(
    status, bound_review_context
):
    record = ReviewVerificationRecord(
        verification_id="V-03",
        check_type="test",
        scenario="focused unit test",
        status=status,
        head_sha=bound_review_context.head_sha,
        environment="local",
        source="local_runner",
        source_ref="pytest tests/test_service.py",
        reason=f"Provider supplied a {status.replace('_', ' ')} result.",
    )
    report = FinalReviewReport(
        summary="Verification was incomplete.",
        profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
        verification_records=(record,),
    )

    rendered = render_final_report(report, context=bound_review_context)

    display_status = status.replace("_", " ")
    assert f"status: {display_status}; head binding: current" in rendered
    assert f"Provider supplied a {display_status} result" in rendered
    assert "status: passed; head binding: current" not in rendered


def test_local_reproduction_provenance_and_artifact_are_rendered(bound_review_context):
    scope = "pytest tests/test_service.py"
    record = ReviewVerificationRecord(
        verification_id="V-04",
        check_type="test",
        scenario="focused unit test",
        status="passed",
        head_sha=bound_review_context.head_sha,
        environment="local",
        source="local_runner",
        source_ref="pytest process 1234",
        scope=scope,
        scope_sha256=hashlib.sha256(scope.encode("utf-8")).hexdigest(),
        run_id="local:run-1234",
        artifact_sha256="6" * 64,
        started_at=datetime(2026, 10, 20, 12, 0, tzinfo=UTC),
        completed_at=datetime(2026, 10, 20, 12, 1, tzinfo=UTC),
        provenance="local_reproduced",
    )
    report = FinalReviewReport(
        summary="A local verification run was recorded.",
        profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
        verification_records=(record,),
    )

    rendered = render_final_report(report, context=bound_review_context)

    assert "provenance: locally reproduced" in rendered
    assert "run ID:" in rendered
    assert "artifact sha256:" in rendered


def test_renderer_shows_original_unconfirmed_candidate_and_refutation_evidence(
    review_finding_candidate, bound_review_context
):
    candidate = review_finding_candidate.model_copy(
        update={
            "title": "Duplicate side effect",
            "description": "A retried request may write the same record twice.",
            "failure_condition": "The first write succeeds but the response is lost before retry.",
            "flip_condition": "A persisted idempotency key prevents a second write.",
        }
    )
    refutation = make_refutation_check(
        bound_review_context,
        candidate,
        status="unverified",
        rationale="The storage-level idempotency contract was not available.",
        evidence_refs=("E2",),
    )
    report = FinalReviewReport(
        summary="One candidate still needs follow-up.",
        profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
        candidate_decisions=(
            ReviewCandidateDecision(
                source=ReviewFindingSource(
                    profile=ReviewAgent.CODE_REVIEWER, candidate_id=candidate.candidate_id
                ),
                verdict="unverified",
                rationale="The available source does not resolve the storage contract.",
                evidence_refs=candidate.evidence_refs,
                candidate_snapshot=candidate,
                refutation=refutation,
            ),
        ),
    )

    rendered = render_final_report(report, context=bound_review_context)

    assert "## Possible bugs not confirmed" in rendered
    assert "A retried request may write the same record twice\\." in rendered
    assert "The first write succeeds but the response is lost before retry\\." in rendered
    assert "A persisted idempotency key prevents a second write\\." in rendered
    assert "storage\\-level idempotency contract was not available" in rendered
    assert "unverified" in rendered
    assert "E2" in rendered


def test_report_context_rejects_refutation_refs_outside_bound_evidence(
    review_finding_candidate, bound_review_context
):
    candidate = review_finding_candidate
    refutation = make_refutation_check(
        bound_review_context,
        candidate,
        status="unverified",
        rationale="The counterevidence could not be inspected.",
        evidence_refs=("E2",),
    )
    report = FinalReviewReport(
        summary="Follow-up is needed.",
        profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
        candidate_decisions=(
            ReviewCandidateDecision(
                source=ReviewFindingSource(
                    profile=ReviewAgent.CODE_REVIEWER, candidate_id=candidate.candidate_id
                ),
                verdict="unverified",
                rationale="The counterevidence could not be inspected.",
                evidence_refs=candidate.evidence_refs,
                candidate_snapshot=candidate,
                refutation=refutation,
            ),
        ),
    )

    with pytest.raises(ValueError, match="outside the bound context"):
        validate_final_report_context(
            report, bound_review_context.model_copy(update={"evidence_refs": ("E1",)})
        )
