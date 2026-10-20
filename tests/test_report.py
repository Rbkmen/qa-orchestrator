import json
from datetime import UTC, datetime

import pytest

from qa_orchestrator.cli import main as cli_main
from qa_orchestrator.contracts import (
    EvidenceRecord,
    FinalReviewFinding,
    FinalReviewReport,
    ProfileAttemptMetrics,
    ProfileChangedPathCoverage,
    ProfileExecutionMetrics,
    ReviewAgent,
    ReviewCandidateDecision,
    ReviewContext,
    ReviewCoverageLedger,
    ReviewCriterionCoverage,
    ReviewFindingLocation,
    ReviewFindingSource,
    ReviewNote,
    ReviewVerificationRecord,
    review_changed_paths_sha256,
)
from qa_orchestrator.refutation import make_refutation_check
from qa_orchestrator.report import render_final_report, validate_final_report_context


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
        next_step="Add a regression test for the boundary input.",
        severity=severity,
        source_profiles=(ReviewAgent.CODE_REVIEWER,),
    )


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
    assert "Counterevidence that would change the conclusion" in rendered
    assert "## Coverage gaps" in rendered
    assert "## Unverified" in rendered


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
        ),
    )

    rendered = render_final_report(report, context=context)

    assert "## Changed-path coverage" in rendered
    assert "Paths with registered head-source evidence: 2/3; paths with verified text excerpts: 1/3." in rendered
    assert 'No registered head-source record: `"docs/README.md"`' in rendered
    assert 'Source text explicitly marked unavailable: `"src/binary.dat"`' in rendered
    assert 'Not explicitly attested as reviewed: `"src/binary.dat"`' in rendered
    assert 'Executor-attested as reviewed: `"src/service.py"`' in rendered
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
        evidence_refs=("R1",),
    )
    report = FinalReviewReport(
        summary="Reviewed and checked the change.",
        profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
        verification_records=(record,),
    )

    validate_final_report_context(report, context)
    rendered = render_final_report(report, context=context)

    assert "## Tests and CI verification" in rendered
    assert "host-supplied records bound to a head SHA" in rendered
    assert "unit and integration test suites" in rendered
    assert "workflow 123 / job 456" in rendered
    assert "<https://github.com/example/repo/actions/runs/123>" in rendered
    assert "passed" in rendered

    wrong_head = report.model_copy(
        update={
            "verification_records": (
                record.model_copy(update={"head_sha": "0" * 40}),
            )
        }
    )
    with pytest.raises(ValueError, match="bound head revision"):
        validate_final_report_context(wrong_head, context)


def test_verification_evidence_requires_head_bound_runtime_record(bound_review_context):
    context = ReviewContext.model_validate(
        {
            **bound_review_context.model_dump(),
            "evidence_refs": ("E1",),
            "evidence_registry": (
                EvidenceRecord(
                    evidence_ref="E1",
                    kind="source",
                    revision=bound_review_context.head_sha,
                    path="src/service.py",
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
                check_type="test",
                scenario="focused unit test",
                status="skipped",
                head_sha=context.head_sha,
                environment="local",
                source="local_runner",
                source_ref="pytest tests/test_service.py",
                reason="The test dependency was unavailable.",
                evidence_refs=("E1",),
            ),
        ),
    )

    with pytest.raises(ValueError, match="runtime record bound to the reviewed head"):
        validate_final_report_context(report, context)


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
