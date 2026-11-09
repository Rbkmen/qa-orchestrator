import hashlib
import json
import subprocess

import pytest

from qa_orchestrator.evidence import evidence_main
from qa_orchestrator.git_evidence import prepare_remote_git_input, resolve_git_input


def test_prepare_remote_fetches_missing_branches_without_touching_worktree(tmp_path):
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    clone = tmp_path / "clone"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", str(work)], check=True)

    def git(path, *args):
        return subprocess.check_output(["git", "-C", str(path), *args]).decode().strip()

    (work / "file").write_text("base\n")
    git(work, "add", "file")
    git(work, "-c", "user.name=Test", "-c", "user.email=t@example.invalid", "commit", "-qm", "base")
    git(work, "branch", "target")
    git(work, "checkout", "-qb", "feature")
    (work / "file").write_text("head\n")
    git(work, "add", "file")
    git(work, "-c", "user.name=Test", "-c", "user.email=t@example.invalid", "commit", "-qm", "head")
    git(work, "push", "-q", str(origin), "target", "feature")
    subprocess.run(["git", "clone", "-q", "--branch", "target", "--single-branch", str(origin), str(clone)], check=True)
    (clone / "file").write_text("uncommitted\n")
    before = git(clone, "status", "--porcelain")
    result = prepare_remote_git_input(clone, "origin", "target", "feature")
    assert result["status"] == "resolved"
    assert result["base_sha"] == git(work, "rev-parse", "target")
    assert result["head_sha"] == git(work, "rev-parse", "feature")
    verified = prepare_remote_git_input(
        clone, "origin", "target", "feature",
        observed_branch_head=result["head_sha"], mr_head=result["head_sha"], mr_base=result["base_sha"],
    )
    assert verified["revision_comparison"]["branch_head"] == "match"
    assert verified["revision_comparison"]["mr_head"] == "match"
    assert verified["revision_comparison"]["mr_base"] == "match"
    different = prepare_remote_git_input(
        clone, "origin", "target", "feature", observed_branch_head="a" * 40,
        mr_head=result["head_sha"], mr_base="b" * 40,
    )
    assert different["revision_comparison"]["branch_head"] == "mismatch"
    assert different["revision_comparison"]["mr_head"] == "match"
    assert different["revision_comparison"]["mr_base"] == "mismatch"
    assert result["revision_comparison"]["branch_head"] == "unavailable"
    assert result["revision_comparison"]["mr_head"] == "unavailable"
    assert result["revision_comparison"]["mr_base"] == "unavailable"
    partial = prepare_remote_git_input(clone, "origin", "target", "feature", mr_head=result["head_sha"])
    assert partial["revision_comparison"]["mr_head"] == "match"
    assert partial["revision_comparison"]["mr_base"] == "unavailable"
    with pytest.raises(ValueError, match="full"):
        prepare_remote_git_input(clone, "origin", "target", "feature", mr_head="1a1011a6")
    assert result["changed_files"] == ["file"]
    assert git(clone, "status", "--porcelain") == before
    assert (clone / "file").read_text() == "uncommitted\n"
    assert git(clone, "branch", "--show-current") == "target"
    assert git(clone, "for-each-ref", "--format=%(refname)", "refs/qa-orchestrator/") == ""
    missing = prepare_remote_git_input(clone, "origin", "target", "missing")
    assert missing["status"] == "fetch_failed"
    assert git(clone, "status", "--porcelain") == before
    assert git(clone, "for-each-ref", "--format=%(refname)", "refs/qa-orchestrator/") == ""


def test_prepare_remote_returns_diff_fallback_when_fetch_fails(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    result = prepare_remote_git_input(tmp_path, "missing", "main", "feature")
    assert result["status"] == "fetch_failed"
    assert "diff" in result["next_action"].lower()


def test_resolves_branch_names_to_immutable_diff(tmp_path, capsys):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args]).strip()

    git("init", "-q")
    (tmp_path / "example.txt").write_text("before\n")
    git("add", "example.txt")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base")
    git("branch", "review-base")
    base = git("rev-parse", "HEAD").decode()
    (tmp_path / "example.txt").write_text("after\n")
    git("add", "example.txt")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "head")
    git("branch", "review-head")
    head = git("rev-parse", "HEAD").decode()
    diff = subprocess.check_output(
        [
            "git",
            "-C",
            str(tmp_path),
            "diff",
            "--binary",
            "--no-ext-diff",
            "--no-textconv",
            base,
            head,
            "--",
        ]
    )

    result = resolve_git_input(tmp_path, "review-base", "review-head")
    assert result == {
        "base_sha": base,
        "head_sha": head,
        "diff_sha256": hashlib.sha256(diff).hexdigest(),
        "changed_files": ["example.txt"],
    }
    assert (
        evidence_main(
            [
                "resolve-git",
                "--repository",
                str(tmp_path),
                "--base",
                "review-base",
                "--head",
                "review-head",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == result
    with pytest.raises(ValueError):
        resolve_git_input(tmp_path, "--help", "review-head")
