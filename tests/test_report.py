import json

from qa_orchestrator.cli import main as cli_main
from qa_orchestrator.contracts import (
    EvidenceRecord,
    FinalReviewFinding,
    FinalReviewReport,
    ReviewAgent,
    ReviewContext,
    ReviewFindingLocation,
    ReviewNote,
)
from qa_orchestrator.report import render_final_report


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
