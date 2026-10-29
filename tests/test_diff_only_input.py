import hashlib
import json

import pytest
from pydantic import ValidationError

from qa_orchestrator.context_pack import build_diff_context_packet, validate_packet_context
from qa_orchestrator.contracts import (
    EvidenceRecord,
    FinalReviewReport,
    ProfileChangedPathCoverage,
    ReviewAgent,
    ReviewContext,
    ReviewCoverageLedger,
    ReviewVerificationRecord,
    review_changed_paths_sha256,
)
from qa_orchestrator.evidence import (
    bind_diff_only,
    evidence_main,
    review_completion_gaps,
    validate_registered_locations,
)
from qa_orchestrator.orchestration import DeepReviewSignals, QaOrchestrator
from qa_orchestrator.report import render_final_report
from qa_orchestrator.service import OrchestratorService


def test_diff_only_context_starts_without_git_revisions(bound_review_context, tmp_path):
    diff = b"diff --git a/src/service.py b/src/service.py\n--- a/src/service.py\n+++ b/src/service.py\n@@ -1 +1 @@\n-old\n+new\n"
    data = bound_review_context.model_dump(mode="json")
    data.update(input_kind="diff", base_sha=None, head_sha=None, diff_sha256=hashlib.sha256(diff).hexdigest())
    data.pop("diff_hunk_inventory_sha256", None)
    data.pop("diff_hunks", None)
    data["evidence_registry"] = [
        {"evidence_ref": "E1", "kind": "diff", "sha256": data["diff_sha256"]},
        {"evidence_ref": "E2", "kind": "other", "sha256": "a" * 64, "locator": "host:context"},
    ]
    data["input_manifest"].update(
        schema_version=2, source_inventory_status="complete", source_conflict_status="clear"
    )
    context = bind_diff_only(ReviewContext.model_validate(data), diff)
    session = OrchestratorService.from_settings(data_dir=tmp_path).start_qa_orchestration(
        "ordinary_review", context, require_review_preflight=True
    )
    assert session.review_context.input_kind == "diff"
    assert session.review_context.base_sha is None
    assert session.review_context.head_sha is None


def test_diff_only_cannot_claim_git_revisions(bound_review_context):
    data = bound_review_context.model_dump(mode="json")
    data["input_kind"] = "diff"
    with pytest.raises(ValidationError):
        ReviewContext.model_validate(data)


def test_diff_only_allows_diff_evidence_without_a_revision(bound_review_context):
    data = bound_review_context.model_dump(mode="json")
    data.update(input_kind="diff", base_sha=None, head_sha=None)
    data["evidence_registry"] = [
        {"evidence_ref": "E1", "kind": "diff", "sha256": data["diff_sha256"]},
        {"evidence_ref": "E2", "kind": "other", "sha256": "a" * 64, "locator": "host:context"},
    ]
    context = ReviewContext.model_validate(data)
    assert context.evidence_registry[0] == EvidenceRecord(
        evidence_ref="E1", kind="diff", sha256=data["diff_sha256"]
    )


def test_diff_only_binds_exact_diff_and_hunks(bound_review_context):
    diff = b"diff --git a/src/service.py b/src/service.py\n--- a/src/service.py\n+++ b/src/service.py\n@@ -1 +1 @@\n-old\n+new\n"
    data = bound_review_context.model_dump(mode="json")
    data.update(
        input_kind="diff",
        base_sha=None,
        head_sha=None,
        diff_sha256=hashlib.sha256(diff).hexdigest(),
    )
    data.pop("diff_hunk_inventory_sha256", None)
    data.pop("diff_hunks", None)
    context = bind_diff_only(ReviewContext.model_validate(data), diff)
    assert context.diff_hunk_inventory_status == "complete"
    assert len(context.diff_hunks) == 1
    with pytest.raises(ValueError, match="hash"):
        bind_diff_only(context, diff + b"tampered")
    truncated = diff.replace(b"+new\n", b"")
    data["diff_sha256"] = hashlib.sha256(truncated).hexdigest()
    with pytest.raises(ValueError, match="truncated"):
        bind_diff_only(ReviewContext.model_validate(data), truncated)


def test_diff_only_finding_must_be_in_a_bound_hunk(bound_review_context, review_finding_candidate):
    diff = b"diff --git a/src/service.py b/src/service.py\n--- a/src/service.py\n+++ b/src/service.py\n@@ -42 +42 @@\n-old\n+new\n"
    data = bound_review_context.model_dump(mode="json")
    data.update(input_kind="diff", base_sha=None, head_sha=None, diff_sha256=hashlib.sha256(diff).hexdigest())
    data.pop("diff_hunk_inventory_sha256", None)
    data.pop("diff_hunks", None)
    data["evidence_registry"] = [
        {"evidence_ref": "E1", "kind": "diff", "sha256": data["diff_sha256"]},
        {"evidence_ref": "E2", "kind": "other", "sha256": "a" * 64, "locator": "host:context"},
    ]
    context = bind_diff_only(ReviewContext.model_validate(data), diff)
    validate_registered_locations(context, (review_finding_candidate,))
    bad = review_finding_candidate.model_copy(update={"source_locations": (review_finding_candidate.source_locations[0].model_copy(update={"start_line": 44}),)})
    with pytest.raises(ValueError, match="within a bound diff hunk"):
        validate_registered_locations(context, (bad,))


def test_diff_only_builds_packet_without_repository(bound_review_context, tmp_path):
    diff = b"diff --git a/src/service.py b/src/service.py\n--- a/src/service.py\n+++ b/src/service.py\n@@ -1 +1 @@\n-old\n+new\n"
    data = bound_review_context.model_dump(mode="json")
    data.update(
        input_kind="diff",
        base_sha=None,
        head_sha=None,
        diff_sha256=hashlib.sha256(diff).hexdigest(),
    )
    data.pop("diff_hunk_inventory_sha256", None)
    data.pop("diff_hunks", None)
    data["evidence_registry"] = [
        {"evidence_ref": "E1", "kind": "diff", "sha256": data["diff_sha256"]},
        {
            "evidence_ref": "E2",
            "kind": "other",
            "sha256": hashlib.sha256(b"context").hexdigest(),
            "locator": "host:context",
        },
    ]
    context = bind_diff_only(ReviewContext.model_validate(data), diff)
    packet = build_diff_context_packet(context, diff, external_content={"E2": "context"})
    validate_packet_context(packet, context)
    assert packet.fragments[0].text == diff.decode()
    context_path = tmp_path / "context.json"
    diff_path = tmp_path / "change.diff"
    content_path = tmp_path / "content.json"
    packet_path = tmp_path / "packet.json"
    bound_path = tmp_path / "bound.json"
    context_path.write_text(context.model_dump_json())
    diff_path.write_bytes(diff)
    content_path.write_text(json.dumps({"E2": "context"}))
    assert evidence_main([
        "pack-diff", "--context", str(context_path), "--diff", str(diff_path),
        "--content", str(content_path), "--packet", str(packet_path),
        "--bound-context", str(bound_path),
    ]) == 0
    bound = ReviewContext.model_validate_json(bound_path.read_bytes())
    assert bound.evidence_packet_sha256 == hashlib.sha256(packet_path.read_bytes()).hexdigest()


def test_complete_diff_only_review_needs_no_git_impact_ledger(
    bound_review_context, make_profile_review_report,
):
    diff = b"diff --git a/src/service.py b/src/service.py\n--- a/src/service.py\n+++ b/src/service.py\n@@ -1 +1 @@\n-old\n+new\n"
    data = bound_review_context.model_dump(mode="json")
    data.update(input_kind="diff", base_sha=None, head_sha=None,
                diff_sha256=hashlib.sha256(diff).hexdigest())
    data.update(diff_hunks=[], diff_hunk_inventory_sha256=None)
    data["input_manifest"].update(schema_version=2, source_inventory_status="complete",
                                  source_conflict_status="clear")
    context = bind_diff_only(ReviewContext.model_validate(data), diff)
    ledger = ReviewCoverageLedger(
        changed_paths_sha256=review_changed_paths_sha256(context.changed_files),
        changed_path_count=1,
        profiles=(ProfileChangedPathCoverage(profile=ReviewAgent.CODE_REVIEWER,
                                             context_selection_recorded=True,
                                             reviewed_diff_hunk_indexes=(0,)),),
        diff_hunk_inventory_sha256=context.diff_hunk_inventory_sha256,
        diff_hunk_count=1,
        unreviewed_diff_hunk_indexes=(),
    )
    report = FinalReviewReport(summary="Reviewed diff.", profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
                               coverage_ledger=ledger)
    assert review_completion_gaps(context, report) == ()
    assert "Git revisions, repository provenance and surrounding source are not verified" in render_final_report(report, context=context)
    incomplete = report.model_copy(update={"coverage_ledger": ledger.model_copy(update={"unreviewed_diff_hunk_indexes": (0,)})})
    assert any("unreviewed diff hunk" in gap for gap in review_completion_gaps(context, incomplete))

    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=1)
    session = orchestrator.start("ordinary_review", context)
    session = orchestrator.advance(
        run_id=session.run_id, completed_step="triage", status="completed",
        selected_profile=ReviewAgent.CODE_REVIEWER,
    )
    session = orchestrator.advance(
        run_id=session.run_id, completed_step="primary_review", status="completed",
        completed_profile=ReviewAgent.CODE_REVIEWER,
        profile_reports=[make_profile_review_report(ReviewAgent.CODE_REVIEWER, context=context)],
        risk_signals=DeepReviewSignals(), skip_synthesis=True,
    )
    final = orchestrator.finish(
        run_id=session.run_id, outcome="completed",
        final_review_report=FinalReviewReport(
            summary="Reviewed diff.", profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
            verification_records=(ReviewVerificationRecord(
                verification_id="V-01", check_type="test", scenario="unit tests",
                status="passed", environment="local", source="local_runner", source_ref="pytest",
            ),),
        ),
    )
    assert final.status.value == "completed"
    rendered = render_final_report(final.final_review_report, context=context)
    assert "head binding: unknown" in rendered
    assert "passed, head binding unknown" in rendered
    known_run = final.final_review_report.verification_records[0].model_copy(
        update={"head_sha": "a" * 40}
    )
    from qa_orchestrator.orchestration import validate_final_report_verification_records
    from qa_orchestrator.report import verification_binding

    known_report = final.final_review_report.model_copy(update={"verification_records": (known_run,)})
    validate_final_report_verification_records(context, known_report)
    assert verification_binding(known_run, context) == "unknown"
