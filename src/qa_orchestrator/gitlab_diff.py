"""Convert a host-supplied, unfiltered GitLab MCP diff to a bounded text diff."""

import hashlib
import re
from typing import Any

from qa_orchestrator.contracts import ReviewContext

_PATH = re.compile(r"^[A-Za-z0-9_./-]+$")
_REF = re.compile(r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+![1-9][0-9]*$")


def import_gitlab_diff(payload: Any, *, unfiltered: bool) -> tuple[bytes, dict]:
    """Validate MCP output; the host attests no path filter and max_files=100."""
    from qa_orchestrator.evidence import bind_diff_only

    if not unfiltered:
        raise ValueError("host must attest an unfiltered request with max_files=100")
    if not isinstance(payload, dict) or not _REF.fullmatch(str(payload.get("ref", ""))):
        raise ValueError("expected a GitLab MCP project!iid response")
    files = payload.get("files")
    stats = payload.get("stats")
    if (payload.get("truncated") is not False or not isinstance(files, list)
            or not 0 < len(files) < 100 or not isinstance(stats, dict)
            or stats.get("files_changed") != len(files)):
        raise ValueError("diff file inventory is incomplete or reaches the one-page limit")
    chunks, changed_paths = [], []
    for item in files:
        if not isinstance(item, dict):
            raise TypeError("invalid diff file entry")
        old, new, patch = item.get("old_path"), item.get("new_path"), item.get("diff")
        if not isinstance(old, str) or not isinstance(new, str) or not _PATH.fullmatch(old) or not _PATH.fullmatch(new):
            raise ValueError("unsupported diff path; obtain a complete unified diff instead")
        if any(part in {"", ".", ".."} for path in (old, new) for part in path.split("/")):
            raise ValueError("invalid diff path")
        if any(type(item.get(key)) is not bool for key in ("new_file", "deleted_file", "renamed_file", "too_large")):
            raise ValueError("missing GitLab file flags")
        if item["too_large"] or item.get("truncated_by_cdc") or not isinstance(patch, str) or not patch.startswith("@@ "):
            raise ValueError("diff file is missing, truncated, binary, or metadata-only")
        if item["new_file"] and item["deleted_file"]:
            raise ValueError("file cannot be both new and deleted")
        additions = sum(line.startswith("+") for line in patch.splitlines())
        deletions = sum(line.startswith("-") for line in patch.splitlines())
        if item.get("additions") != additions or item.get("deletions") != deletions:
            raise ValueError("diff line counts do not match GitLab MCP metadata")
        changed_paths.append(new)
        before = "/dev/null" if item["new_file"] else f"a/{old}"
        after = "/dev/null" if item["deleted_file"] else f"b/{new}"
        chunks.append(f"diff --git a/{old} b/{new}\n--- {before}\n+++ {after}\n{patch.rstrip(chr(10))}\n")
    if len(set(changed_paths)) != len(changed_paths):
        raise ValueError("duplicate changed paths")
    diff = "".join(chunks).encode("utf-8")
    context = ReviewContext(
        repository=payload["ref"].rsplit("!", 1)[0], input_kind="diff",
        diff_sha256=hashlib.sha256(diff).hexdigest(), evidence_packet_sha256="0" * 64,
        changed_files=changed_paths, evidence_refs=("D1",),
    )
    bound = bind_diff_only(context, diff)
    return diff, {
        "ref": payload["ref"], "changed_files": changed_paths,
        "diff_sha256": bound.diff_sha256, "hunks": len(bound.diff_hunks),
        "scope": "host-attested unfiltered MCP response; Git revisions and server-side omissions unverified",
    }
