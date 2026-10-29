"""Read and verify evidence from pinned Git objects."""

import hashlib
import subprocess
from pathlib import Path

from qa_orchestrator.contracts import EvidenceRecord, ReviewContext


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
