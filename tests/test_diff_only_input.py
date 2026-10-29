import hashlib
import json

import pytest
from pydantic import ValidationError

from qa_orchestrator.context_pack import build_diff_context_packet, validate_packet_context
from qa_orchestrator.contracts import EvidenceRecord, ReviewContext
from qa_orchestrator.evidence import bind_diff_only, evidence_main, validate_registered_locations
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
