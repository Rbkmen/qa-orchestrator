"""Read and verify evidence from pinned Git objects."""

import hashlib
import os
import re
import subprocess
import uuid
from pathlib import Path

from qa_orchestrator.contracts import EvidenceRecord, ReviewContext


def prepare_remote_git_input(
    repository: Path, remote: str, target: str, source: str, *,
    observed_branch_head: str | None = None, mr_head: str | None = None, mr_base: str | None = None,
) -> dict[str, object]:
    """Fetch two named remote branches into temporary refs, leaving the worktree untouched."""
    if not repository.is_absolute() or not remote or remote.startswith("-"):
        raise ValueError("absolute repository path and configured remote name required")
    for revision in (observed_branch_head, mr_head, mr_base):
        if revision is not None and not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision):
            raise ValueError("observed revisions must be full lowercase Git object IDs")
    remotes = _git(repository, "remote").decode("utf-8").splitlines()
    if remote not in remotes:
        return {"status": "fetch_failed", "reason": "configured remote not found",
                "next_action": "Continue with the complete GitLab diff if available."}
    for branch in (target, source):
        if not branch or branch.startswith(("-", "refs/")):
            raise ValueError("expected remote branch names")
        _git(repository, "check-ref-format", f"refs/heads/{branch}")
    namespace = f"refs/qa-orchestrator/{uuid.uuid4().hex}"
    target_ref, source_ref = f"{namespace}/target", f"{namespace}/source"
    command = ["git", "-C", str(repository), "-c", "maintenance.auto=false", "fetch",
               "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head", "--",
               remote, f"refs/heads/{target}:{target_ref}", f"refs/heads/{source}:{source_ref}"]
    try:
        try:
            result = subprocess.run(command, capture_output=True, check=False, timeout=60,
                                    env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        except (OSError, subprocess.TimeoutExpired):
            return {"status": "fetch_failed", "reason": "fetch could not complete",
                    "next_action": "Continue with the complete GitLab diff if available."}
        if result.returncode:
            return {"status": "fetch_failed", "reason": "fetch failed",
                    "next_action": "Continue with the complete GitLab diff if available."}
        target_sha = _git(repository, "rev-parse", "--verify", target_ref).decode("ascii").strip()
        head_sha = _git(repository, "rev-parse", "--verify", source_ref).decode("ascii").strip()
        try:
            base_sha = _git(repository, "merge-base", target_sha, head_sha).decode("ascii").strip()
            result_data = resolve_git_input(repository, base_sha, head_sha)
        except ValueError:
            return {"status": "comparison_unavailable", "reason": "merge base or diff unavailable",
                    "next_action": "Continue with the complete GitLab diff if available."}
        def compare(observed: str | None, actual: str) -> str:
            return "unavailable" if observed is None else ("match" if observed == actual else "mismatch")

        return {"status": "resolved", "target_sha": target_sha, **result_data,
                "revision_comparison": {
                    "branch_head": compare(observed_branch_head, head_sha),
                    "mr_head": compare(mr_head, head_sha),
                    "mr_base": compare(mr_base, base_sha),
                    "scope": "Host-supplied observations; patch identity, provenance and freshness are not verified.",
                },
                "comparison_note": "Local merge base; verify it matches the authoritative MR comparison."}
    finally:
        for ref in (target_ref, source_ref):
            subprocess.run(["git", "-C", str(repository), "update-ref", "-d", ref],
                           capture_output=True, check=False)


def _git(repository: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(repository), *args], capture_output=True, check=False)
    if result.returncode:
        raise ValueError("pinned evidence object is unavailable in the local repository")
    return result.stdout


def resolve_git_input(repository: Path, base: str, head: str) -> dict[str, object]:
    """Resolve local refs once and describe the exact immutable Git diff."""
    revisions = []
    for ref in (base, head):
        if not ref or ref.startswith("-") or "\x00" in ref:
            raise ValueError("invalid Git revision")
        revisions.append(
            _git(repository, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
            .decode("ascii")
            .strip()
        )
    base_sha, head_sha = revisions
    if base_sha == head_sha:
        raise ValueError("base and head must identify different commits")
    diff = _git(
        repository, "diff", "--binary", "--no-ext-diff", "--no-textconv", base_sha, head_sha, "--"
    )
    paths = _git(repository, "diff", "--name-only", "-z", base_sha, head_sha, "--")
    try:
        changed_files = [path.decode("utf-8") for path in paths.split(b"\0") if path]
    except UnicodeDecodeError as exc:
        raise ValueError("changed file paths must be valid UTF-8") from exc
    if not changed_files:
        raise ValueError("Git diff has no changed files")
    return {
        "base_sha": base_sha,
        "head_sha": head_sha,
        "diff_sha256": hashlib.sha256(diff).hexdigest(),
        "changed_files": changed_files,
    }


def _diff(context: ReviewContext, repository: Path) -> bytes:
    for revision in (context.base_sha, context.head_sha):
        if _git(repository, "cat-file", "-t", revision).strip() != b"commit":
            raise ValueError("bound revisions must identify commits")
    data = _git(
        repository,
        "diff",
        "--binary",
        "--no-ext-diff",
        "--no-textconv",
        context.base_sha,
        context.head_sha,
        "--",
    )
    if hashlib.sha256(data).hexdigest() != context.diff_sha256:
        raise ValueError("local diff does not match the bound diff hash")
    changed = _git(
        repository,
        "diff",
        "--name-only",
        "-z",
        context.base_sha,
        context.head_sha,
        "--",
    )
    try:
        changed_paths = {path.decode("utf-8") for path in changed.split(b"\0") if path}
    except UnicodeDecodeError as exc:
        raise ValueError("changed file paths must be valid UTF-8") from exc
    if changed_paths != set(context.changed_files):
        raise ValueError("bound changed_files do not match the pinned diff paths")
    return data


def _source_bytes(entry: EvidenceRecord, repository: Path) -> bytes:
    if _git(repository, "cat-file", "-t", f"{entry.revision}:{entry.path}").strip() != b"blob":
        raise ValueError("source evidence must identify a file blob")
    data = _git(repository, "show", f"{entry.revision}:{entry.path}")
    if entry.start_line is None:
        return data
    lines = _source_lines(data)
    end_line = entry.end_line or entry.start_line
    if end_line > len(lines):
        raise ValueError(f"evidence source line range is out of bounds: {entry.evidence_ref}")
    return b"".join(lines[entry.start_line - 1 : end_line])


def _source_lines(data: bytes) -> list[bytes]:
    parts = data.split(b"\n")
    return [part + b"\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def _is_text_source(data: bytes) -> bool:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return "\x00" not in text
