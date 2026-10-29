import copy
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
