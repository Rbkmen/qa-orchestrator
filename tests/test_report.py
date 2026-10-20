import json

import pytest

from qa_orchestrator.cli import main as cli_main
from qa_orchestrator.contracts import (
    EvidenceRecord,
    FinalReviewFinding,
    FinalReviewReport,
    ReviewAgent,
    ReviewCandidateDecision,
    ReviewContext,
    ReviewFindingLocation,
    ReviewFindingSource,
    ReviewNote,
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
