"""Convert a host-supplied, unfiltered GitLab MCP diff to a bounded text diff."""

import hashlib
import re
from typing import Any
from urllib.parse import urlsplit

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


def prepare_gitlab_review(payload: Any, spec: Any, *, unfiltered: bool):
    """Build a host-owned diff packet; source assessments are supplied by the host."""
    from qa_orchestrator.context_pack import build_diff_context_packet
    from qa_orchestrator.evidence import bind_diff_only

    diff, summary = import_gitlab_diff(payload, unfiltered=unfiltered)
    if not isinstance(spec, dict) or set(spec) != {
        "input_manifest", "current_merge_request_source_id", "evidence"
    } or not isinstance(spec["evidence"], list):
        raise ValueError("spec must contain input_manifest, current_merge_request_source_id and evidence")
    registry = [{"evidence_ref": "D1", "kind": "diff", "sha256": summary["diff_sha256"]}]
    content = {}
    for item in spec["evidence"]:
        if not isinstance(item, dict) or set(item) != {"evidence_ref", "kind", "locator", "text"}:
            raise ValueError("each evidence item requires evidence_ref, kind, locator and text")
        if item["kind"] not in {"requirement", "claim", "other"} or not isinstance(item["text"], str):
            raise ValueError("evidence must be a requirement, claim or other text")
        ref = item["evidence_ref"]
        if ref == "D1" or ref in content:
            raise ValueError("evidence refs must be unique; D1 is reserved for the diff")
        content[ref] = item["text"]
        registry.append({"evidence_ref": ref, "kind": item["kind"], "locator": item["locator"],
                         "sha256": hashlib.sha256(item["text"].encode("utf-8")).hexdigest()})
    context = ReviewContext.model_validate({
        "repository": payload["ref"].rsplit("!", 1)[0], "input_kind": "diff",
        "diff_sha256": summary["diff_sha256"], "evidence_packet_sha256": "0" * 64,
        "changed_files": summary["changed_files"], "evidence_refs": [r["evidence_ref"] for r in registry],
        "evidence_registry": registry, "input_manifest": spec["input_manifest"],
        "current_merge_request_source_id": spec["current_merge_request_source_id"],
    })
    manifest = context.input_manifest
    if manifest is None or manifest.schema_version != 2 or manifest.expected_source_kinds is None:
        raise ValueError("spec requires a schema v2 manifest and expected source kinds")
    if "merge_request" not in manifest.expected_source_kinds:
        raise ValueError("expected source kinds must include the merge request")
    required = [source for source in manifest.sources if source.required]
    if set(manifest.expected_source_kinds) - {source.kind for source in required}:
        raise ValueError("expected sources must be declared as required")
    mr = next((source for source in required if source.source_id == context.current_merge_request_source_id
               and source.kind == "merge_request"), None)
    project, iid = payload["ref"].rsplit("!", 1)
    if mr is None or not (mr.locator == payload["ref"] or
                          urlsplit(mr.locator).path.rstrip("/") == f"/{project}/-/merge_requests/{iid}"):
        raise ValueError("current merge request must match the diff reference")
    kinds = {entry.evidence_ref: entry.kind for entry in context.evidence_registry}
    for source in required:
        expected = "requirement" if source.kind in {"task", "issue", "jira"} else (
            "claim" if source.kind == "merge_request" else None)
        if expected and source.retrieval_status == "complete" and source.extraction_status == "complete" and source.criteria_status == "unknown":
            raise ValueError(f"criteria assessment required for {source.source_id}")
        if expected and any(kinds.get(ref) != expected for ref in source.criterion_refs):
            raise ValueError(f"criteria for {source.source_id} must be registered as {expected}")
    context = bind_diff_only(context, diff)
    packet = build_diff_context_packet(context, diff, external_content=content)
    packet_bytes = (packet.model_dump_json() + "\n").encode("utf-8")
    context = ReviewContext.model_validate({**context.model_dump(),
        "evidence_packet_sha256": hashlib.sha256(packet_bytes).hexdigest()})
    return diff, packet_bytes, context, summary


def compare_gitlab_diff(payload: Any, expected_ref: str, expected_sha256: str, *, unfiltered: bool) -> dict:
    """Compare independently retrieved patches without inferring their Git revision."""
    _, summary = import_gitlab_diff(payload, unfiltered=unfiltered)
    if summary["ref"] != expected_ref:
        raise ValueError("merge request reference does not match")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("expected diff hash must be a SHA-256 digest")
    return {"ref": expected_ref, "diff_changed": summary["diff_sha256"] != expected_sha256,
            "revision_status": "unverified", "scope": summary["scope"]}
