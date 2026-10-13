"""Host-local evidence construction and verification against pinned Git objects."""

import argparse
import hashlib
import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

from qa_orchestrator.contracts import (
    EvidenceRecord,
    FinalReviewReport,
    ProfileReviewReport,
    ReviewContext,
    ReviewFindingCandidate,
)


def _git(repository: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(repository), *args], capture_output=True, check=False)
    if result.returncode:
        raise ValueError("pinned evidence object is unavailable in the local repository")
    return result.stdout


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


def register_evidence(
    context: ReviewContext,
    records: Sequence[dict],
    repository: Path,
) -> ReviewContext:
    if not isinstance(records, (list, tuple)) or any(
        not isinstance(record, dict) for record in records
    ):
        raise ValueError("evidence records must be a list of metadata objects")
    diff = _diff(context, repository)
    entries = []
    for record in records:
        entry = EvidenceRecord.model_validate({"sha256": "0" * 64, **record})
        if entry.kind == "source":
            data = _source_bytes(entry, repository)
        elif entry.kind == "diff":
            data = diff
        else:
            if "sha256" not in record:
                raise ValueError("external evidence requires a host-supplied content hash")
            entries.append(entry)
            continue
        entries.append(entry.model_copy(update={"sha256": hashlib.sha256(data).hexdigest()}))
    return ReviewContext.model_validate({**context.model_dump(), "evidence_registry": entries})


def validate_registered_locations(
    context: ReviewContext,
    candidates: Sequence[ReviewFindingCandidate],
) -> None:
    if not context.evidence_registry:
        return
    registry = {entry.evidence_ref: entry for entry in context.evidence_registry}
    for candidate in candidates:
        source_entries = [
            registry[ref]
            for ref in candidate.evidence_refs
            if ref in registry
            and registry[ref].kind == "source"
            and registry[ref].revision == context.head_sha
        ]
        for location in candidate.source_locations:
            if not any(
                entry.path == location.path
                and (
                    entry.start_line is None
                    or (
                        location.start_line >= entry.start_line
                        and (location.end_line or location.start_line)
                        <= (entry.end_line or entry.start_line)
                    )
                )
                for entry in source_entries
            ):
                raise ValueError(
                    "finding location is not supported by registered head source evidence"
                )


def verify_evidence(
    context: ReviewContext,
    repository: Path,
    packet: Path | None = None,
    candidates: Sequence[ReviewFindingCandidate] = (),
) -> dict:
    if not context.evidence_registry:
        raise ValueError("evidence_registry is required for local verification")
    diff = _diff(context, repository)
    verified, unverified = [], []
    for entry in context.evidence_registry:
        if entry.kind == "source":
            data = _source_bytes(entry, repository)
        elif entry.kind == "diff":
            data = diff
        else:
            unverified.append(entry.evidence_ref)
            continue
        if hashlib.sha256(data).hexdigest() != entry.sha256:
            raise ValueError(f"evidence content hash mismatch: {entry.evidence_ref}")
        verified.append(entry.evidence_ref)
    if (
        packet is not None
        and hashlib.sha256(packet.read_bytes()).hexdigest() != context.evidence_packet_sha256
    ):
        raise ValueError("Evidence Packet hash mismatch")
    validate_registered_locations(context, candidates)
    for candidate in candidates:
        for location in candidate.source_locations:
            data = _git(repository, "show", f"{context.head_sha}:{location.path}")
            if (location.end_line or location.start_line) > len(_source_lines(data)):
                raise ValueError("finding source location is outside the pinned head file")
    return {
        "head_sha": context.head_sha,
        "verified_refs": verified,
        "unverified_external_refs": unverified,
        "packet_hash_verified": True if packet else None,
        "finding_locations_checked": sum(len(c.source_locations) for c in candidates),
    }


def evidence_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="qa-orch evidence")
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build", help="Build a registry from host-provided ref metadata")
    verify = subparsers.add_parser(
        "verify", help="Check evidence against immutable local Git objects"
    )
    for command in (build, verify):
        command.add_argument("--context", required=True, type=Path)
        command.add_argument("--repository", required=True, type=Path)
        command.add_argument("--output", type=Path)
    build.add_argument("--records", required=True, type=Path)
    verify.add_argument("--packet", type=Path)
    verify.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    context = ReviewContext.model_validate_json(args.context.read_bytes())
    if args.command == "build":
        payload = register_evidence(
            context, json.loads(args.records.read_text()), args.repository
        ).model_dump(mode="json")
    else:
        candidates = ()
        if args.report:
            document = json.loads(args.report.read_text())
            report = (
                ProfileReviewReport if "profile" in document else FinalReviewReport
            ).model_validate(document)
            candidates = report.finding_candidates
        payload = verify_evidence(context, args.repository, args.packet, candidates)
    output = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")
    return 0
