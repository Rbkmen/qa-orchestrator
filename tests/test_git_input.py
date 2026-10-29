import hashlib
import json
import subprocess

import pytest

from qa_orchestrator.evidence import evidence_main
from qa_orchestrator.git_evidence import resolve_git_input


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
