import copy
import hashlib
import json

import pytest

from qa_orchestrator.evidence import evidence_main
from qa_orchestrator.gitlab_diff import import_gitlab_diff


@pytest.fixture
def mcp_diff():
    return {
        "ref": "group/service!123", "truncated": False, "stats": {"files_changed": 1},
        "files": [{"old_path": "src/a.py", "new_path": "src/a.py", "new_file": False,
                   "renamed_file": False, "deleted_file": False, "too_large": False,
                   "additions": 1, "deletions": 1, "diff": "@@ -1 +1 @@\n-old\n+new\n"}],
    }


def test_import_unfiltered_mcp_diff_and_cli(mcp_diff, tmp_path, capsys):
    diff, summary = import_gitlab_diff(mcp_diff, unfiltered=True)
    assert diff.startswith(b"diff --git a/src/a.py b/src/a.py\n")
    assert summary["hunks"] == 1
    assert summary["changed_files"] == ["src/a.py"]
    input_path, output_path = tmp_path / "input.json", tmp_path / "change.diff"
    input_path.write_text(json.dumps(mcp_diff))
    assert evidence_main(["import-gitlab-diff", "--input", str(input_path),
                          "--output", str(output_path), "--unfiltered"]) == 0
    assert output_path.read_bytes() == diff
    assert "server-side omissions unverified" in capsys.readouterr().out
    with pytest.raises(FileExistsError):
        evidence_main(["import-gitlab-diff", "--input", str(input_path),
                       "--output", str(output_path), "--unfiltered"])


@pytest.mark.parametrize("change", ["truncated", "too_large", "missing_diff", "truncated_hunk",
                                    "path", "flags", "counts", "page_limit"])
def test_import_refuses_incomplete_or_unsupported_diff(mcp_diff, change):
    payload = copy.deepcopy(mcp_diff)
    item = payload["files"][0]
    if change == "truncated":
        payload["truncated"] = True
    elif change == "too_large":
        item["too_large"] = True
    elif change == "missing_diff":
        item["diff"] = ""
    elif change == "truncated_hunk":
        item["diff"] = "@@ -1 +1,2 @@\n-old\n+new\n"
    elif change == "path":
        item["new_path"] = "../unsafe.py"
    elif change == "flags":
        del item["too_large"]
    elif change == "counts":
        item["additions"] = 2
    else:
        payload["files"] *= 100
        payload["stats"]["files_changed"] = 100
    with pytest.raises(ValueError):
        import_gitlab_diff(payload, unfiltered=True)


def test_import_requires_unfiltered_host_attestation(mcp_diff):
    with pytest.raises(ValueError, match="unfiltered"):
        import_gitlab_diff(mcp_diff, unfiltered=False)


@pytest.mark.parametrize("kind", ["new", "deleted", "renamed"])
def test_import_preserves_text_file_changes(mcp_diff, kind):
    item = mcp_diff["files"][0]
    if kind == "new":
        item.update(new_file=True, additions=1, deletions=0, diff="@@ -0,0 +1 @@\n+new\n")
    elif kind == "deleted":
        item.update(deleted_file=True, additions=0, deletions=1, diff="@@ -1 +0,0 @@\n-old\n")
    else:
        item.update(renamed_file=True, new_path="src/b.py")
    diff, summary = import_gitlab_diff(mcp_diff, unfiltered=True)
    assert summary["changed_files"] == [item["new_path"]]
    if kind == "new":
        assert b"--- /dev/null\n" in diff
    elif kind == "deleted":
        assert b"+++ /dev/null\n" in diff
    else:
        assert b"diff --git a/src/a.py b/src/b.py\n" in diff


def test_prepare_gitlab_builds_startable_bound_packet(mcp_diff, tmp_path):
    from qa_orchestrator.contracts import ReviewContext
    from qa_orchestrator.service import OrchestratorService

    source = {"source_id": "MR", "kind": "merge_request", "locator": "group/service!123",
              "required": True, "retrieval_status": "complete", "pagination_status": "complete",
              "extraction_status": "complete", "criteria_status": "recorded", "criterion_refs": ["C1"]}
    spec = {"input_manifest": {"schema_version": 2, "expected_source_kinds": ["merge_request"],
             "source_inventory_status": "complete", "source_conflict_status": "clear", "sources": [source]},
            "current_merge_request_source_id": "MR",
            "evidence": [{"evidence_ref": "C1", "kind": "claim", "locator": "host:mr", "text": "Change behavior"}]}
    (tmp_path / "diff.json").write_text(json.dumps(mcp_diff))
    (tmp_path / "spec.json").write_text(json.dumps(spec))
    assert evidence_main(["prepare-gitlab", "--input", str(tmp_path / "diff.json"),
                          "--spec", str(tmp_path / "spec.json"), "--output-dir", str(tmp_path / "out"),
                          "--unfiltered"]) == 0
    out = tmp_path / "out"
    context = ReviewContext.model_validate_json((out / "context.json").read_bytes())
    assert context.input_kind == "diff" and context.head_sha is None
    assert context.diff_hunk_inventory_status == "complete"
    assert context.diff_sha256 == hashlib.sha256((out / "change.diff").read_bytes()).hexdigest()
    assert context.evidence_packet_sha256 == hashlib.sha256((out / "packet.json").read_bytes()).hexdigest()
    assert OrchestratorService.from_settings(data_dir=tmp_path).start_qa_orchestration(
        "ordinary_review", context, require_review_preflight=True).current_step == "triage"


def test_prepare_gitlab_rejects_incomplete_diff_without_writing(mcp_diff, tmp_path):
    mcp_diff["truncated"] = True
    (tmp_path / "diff.json").write_text(json.dumps(mcp_diff))
    (tmp_path / "spec.json").write_text("{}")
    with pytest.raises(ValueError, match="incomplete"):
        evidence_main(["prepare-gitlab", "--input", str(tmp_path / "diff.json"),
                       "--spec", str(tmp_path / "spec.json"), "--output-dir", str(tmp_path / "out"),
                       "--unfiltered"])
    assert not (tmp_path / "out").exists()


def test_prepare_refuses_mismatched_merge_request(mcp_diff):
    from qa_orchestrator.gitlab_diff import prepare_gitlab_review

    spec = {"input_manifest": {"schema_version": 2, "expected_source_kinds": ["merge_request"],
            "source_inventory_status": "partial", "source_conflict_status": "unknown", "sources": [
                {"source_id": "MR", "kind": "merge_request", "locator": "group/service!999", "required": True}]},
            "current_merge_request_source_id": "MR", "evidence": []}
    with pytest.raises(ValueError, match="merge request.*diff"):
        prepare_gitlab_review(mcp_diff, spec, unfiltered=True)


def test_prepare_refuses_unassessed_available_merge_request(mcp_diff):
    from qa_orchestrator.gitlab_diff import prepare_gitlab_review

    spec = {"input_manifest": {"schema_version": 2, "expected_source_kinds": ["merge_request"],
            "source_inventory_status": "partial", "source_conflict_status": "unknown", "sources": [
                {"source_id": "MR", "kind": "merge_request", "locator": "group/service!123", "required": True,
                 "retrieval_status": "complete", "pagination_status": "complete", "extraction_status": "complete"}]},
            "current_merge_request_source_id": "MR", "evidence": []}
    with pytest.raises(ValueError, match="criteria"):
        prepare_gitlab_review(mcp_diff, spec, unfiltered=True)


def test_prepare_preserves_existing_output_directory(mcp_diff, tmp_path):
    from qa_orchestrator.gitlab_diff import prepare_gitlab_review

    spec = {"input_manifest": {"schema_version": 2, "expected_source_kinds": ["merge_request"],
            "source_inventory_status": "partial", "source_conflict_status": "unknown", "sources": [
                {"source_id": "MR", "kind": "merge_request", "locator": "group/service!123", "required": True,
                 "retrieval_status": "complete", "pagination_status": "complete", "extraction_status": "complete",
                 "criteria_status": "none"}]}, "current_merge_request_source_id": "MR", "evidence": []}
    prepare_gitlab_review(mcp_diff, spec, unfiltered=True)
    (tmp_path / "diff.json").write_text(json.dumps(mcp_diff))
    (tmp_path / "spec.json").write_text(json.dumps(spec))
    out = tmp_path / "out"
    out.mkdir()
    (out / "packet.json").write_text("keep")
    with pytest.raises(FileExistsError):
        evidence_main(["prepare-gitlab", "--input", str(tmp_path / "diff.json"),
                       "--spec", str(tmp_path / "spec.json"), "--output-dir", str(out), "--unfiltered"])
    assert (out / "packet.json").read_text() == "keep"


def test_compare_gitlab_detects_patch_change_without_claiming_revision(mcp_diff, tmp_path):
    from qa_orchestrator.gitlab_diff import compare_gitlab_diff

    _, summary = import_gitlab_diff(mcp_diff, unfiltered=True)
    unchanged = compare_gitlab_diff(mcp_diff, "group/service!123", summary["diff_sha256"], unfiltered=True)
    assert unchanged["diff_changed"] is False
    assert unchanged["revision_status"] == "unverified"
    changed = copy.deepcopy(mcp_diff)
    changed["files"][0]["diff"] = "@@ -1 +1 @@\n-old\n+other\n"
    assert compare_gitlab_diff(changed, "group/service!123", summary["diff_sha256"], unfiltered=True)["diff_changed"] is True
    path = tmp_path / "refreshed.json"
    path.write_text(json.dumps(changed))
    assert evidence_main(["compare-gitlab", "--input", str(path), "--ref", "group/service!123",
                          "--diff-sha256", summary["diff_sha256"], "--unfiltered"]) == 1
    with pytest.raises(ValueError, match="reference"):
        compare_gitlab_diff(changed, "group/service!999", summary["diff_sha256"], unfiltered=True)
