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


def validate_context_selection(context: ReviewContext, report: ProfileReviewReport) -> None:
    selection = report.context_selection
    if selection is None:
        return
    if selection.source_packet_sha256 != context.evidence_packet_sha256:
        raise ValueError("context selection packet hash mismatch")
    if set(selection.included_refs) | set(selection.omitted_refs) != set(context.evidence_refs):
        raise ValueError("context selection must account for all bound refs")
    mandatory = {
        entry.evidence_ref for entry in context.evidence_registry
        if entry.kind in {"diff", "requirement"}
        or entry.kind == "source" and entry.path in context.changed_files
    }
    if mandatory - set(selection.included_refs):
        raise ValueError("context selection omits mandatory evidence")
    cited = {ref for f in report.finding_candidates for ref in f.evidence_refs}
    cited.update(ref for note in (*report.coverage_gaps, *report.unverified) for ref in note.evidence_refs)
    cited.update(item.evidence_ref for item in report.evidence_map)
    if cited - set(selection.included_refs):
        raise ValueError("profile report cites evidence omitted from its context")


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
    chunks = subparsers.add_parser(
        "chunks", help="Read pinned source into byte-faithful AST chunks"
    )
    chunks.add_argument("--repository", required=True, type=Path)
    chunks.add_argument("--revision", required=True)
    chunks.add_argument("--path", required=True)
    chunks.add_argument("--max-bytes", default=16000, type=int)
    chunks.add_argument("--output", type=Path)
    pack = subparsers.add_parser("pack", help="Build exact profile-selectable context from pinned refs")
    pack.add_argument("--plan", type=Path, help="Explicit host plan: optional_profiles, priorities, external_content")
    pack.add_argument("--bound-context", required=True, type=Path)
    for command in (build, verify, pack):
        command.add_argument("--context", required=True, type=Path)
        command.add_argument("--repository", required=True, type=Path)
        command.add_argument("--output", type=Path)
    build.add_argument("--records", required=True, type=Path)
    verify.add_argument("--packet", type=Path)
    verify.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if args.command == "chunks":
        from qa_orchestrator.review_chunks import chunk_source

        entry = EvidenceRecord(
            evidence_ref="source",
            kind="source",
            sha256="0" * 64,
            revision=args.revision,
            path=args.path,
        )
        if _git(args.repository, "cat-file", "-t", entry.revision).strip() != b"commit":
            raise ValueError("chunk revision must identify a commit")
        source = _source_bytes(entry, args.repository)
        payload = {
            "revision": entry.revision,
            "path": entry.path,
            "source_sha256": hashlib.sha256(source).hexdigest(),
            "chunks": [
                c.model_dump(mode="json")
                for c in chunk_source(source, path=entry.path, max_bytes=args.max_bytes)
            ],
        }
        output = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.write_text(output, encoding="utf-8")
        else:
            print(output, end="")
        return 0
    context = ReviewContext.model_validate_json(args.context.read_bytes())
    if args.command == "pack":
        from qa_orchestrator.context_pack import build_context_packet

        if args.output is None or args.output.resolve() == args.bound_context.resolve():
            raise ValueError("pack requires distinct output and bound-context paths")
        plan = json.loads(args.plan.read_text()) if args.plan else {}
        packet = build_context_packet(context, args.repository, **plan)
        data = packet.model_dump_json().encode("utf-8")
        if len(data) > 2_000_000:
            raise ValueError("context packet exceeds executor input size")
        bound = ReviewContext.model_validate({
            **context.model_dump(), "evidence_packet_sha256": hashlib.sha256(data).hexdigest()
        })
        args.output.write_bytes(data)
        args.bound_context.write_text(bound.model_dump_json(), encoding="utf-8")
        return 0
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
