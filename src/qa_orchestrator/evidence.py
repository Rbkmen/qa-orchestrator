"""Host-local evidence construction and verification against pinned Git objects."""

import argparse
import ast
import hashlib
import json
import re
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

_DISCOVERY_MAX_SYMBOLS = 128
_DISCOVERY_MAX_TRACKED_PATHS = 200_000
_DISCOVERY_MAX_SOURCE_BYTES = 4_000_000
_DISCOVERY_MAX_TOTAL_BYTES = 64_000_000
_DISCOVERY_MAX_CANDIDATES = 200
_IDENTIFIER = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]{2,127}$")
_IGNORED_IDENTIFIERS = {
    "call", "get", "init", "main", "run", "set", "test", "toString", "valueOf",
}
_IGNORED_PATH_TOKENS = {"app", "lib", "pkg", "packages", "spec", "specs", "src", "test", "tests"}
_DECLARATION_PATTERNS = (
    re.compile(
        r"(?:^|\n)\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?"
        r"(?:function|class|interface|type|enum|const|let|var)\s+([A-Za-z_$][\w$]*)",
        re.MULTILINE,
    ),
    re.compile(r"(?:^|\n)\s*(?:async\s+)?def\s+([A-Za-z_]\w*)", re.MULTILINE),
    re.compile(r"(?:^|\n)\s*(?:public\s+)?(?:func|type)\s+([A-Za-z_]\w*)", re.MULTILINE),
    re.compile(r"(?:^|\n)\s*(?:def|class|module)\s+([A-Za-z_]\w*)", re.MULTILINE),
)
_TEST_PATH = re.compile(
    r"(^|/)(tests?|specs?)(/|$)|(^|[._-])(test|spec)([._-]|$)", re.IGNORECASE
)
_CONTRACT_PATH = re.compile(
    r"(^|/)(contracts?|schemas?|protocols?|types?|interfaces?|openapi|graphql|proto)(/|$)",
    re.IGNORECASE,
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


def _defined_symbols(path: str, source: bytes) -> tuple[str, ...]:
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError:
        return ()
    names: set[str] = set()
    if path.endswith(".py"):
        try:
            tree = ast.parse(text)
        except SyntaxError:
            pass
        else:
            names.update(
                node.name
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            )
    if not names:
        for pattern in _DECLARATION_PATTERNS:
            names.update(pattern.findall(text))
    return tuple(
        sorted(
            (
                name
                for name in names
                if _IDENTIFIER.fullmatch(name) and name not in _IGNORED_IDENTIFIERS
            ),
            key=lambda name: (-len(name), name),
        )[:_DISCOVERY_MAX_SYMBOLS]
    )


def _neighbor_categories(path: str) -> tuple[str, ...]:
    tags = []
    if _TEST_PATH.search(path):
        tags.append("test")
    if _CONTRACT_PATH.search(path):
        tags.append("contract")
    return tuple(tags or ("caller",))


def _path_tokens(path: str) -> set[str]:
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", path)
    return {
        part.lower()
        for part in re.findall(r"[A-Za-z0-9]+", separated)
        if len(part) >= 3 and part.lower() not in _IGNORED_PATH_TOKENS
    }


def discover_neighbor_evidence(
    context: ReviewContext,
    repository: Path,
    *,
    max_candidates: int = 50,
) -> dict:
    """Suggest pinned callers, contract files and conventionally named tests.

    This is a bounded Git text scan. It deliberately returns suggestions only: it
    does not add them to the bound context or claim that the result is exhaustive.
    """
    if not 1 <= max_candidates <= _DISCOVERY_MAX_CANDIDATES:
        raise ValueError("neighbor candidate limit must be between 1 and 200")
    _diff(context, repository)
    if _git(repository, "cat-file", "-t", context.head_sha).strip() != b"commit":
        raise ValueError("bound head revision must identify a commit")

    tracked_raw = _git(repository, "ls-tree", "-r", "--name-only", "-z", context.head_sha)
    if len(tracked_raw) > _DISCOVERY_MAX_TOTAL_BYTES:
        raise ValueError("neighbor scan exceeds the tracked-path output limit")
    tracked = [part.decode("utf-8") for part in tracked_raw.split(b"\0") if part]
    if len(tracked) > _DISCOVERY_MAX_TRACKED_PATHS:
        raise ValueError("neighbor scan exceeds the tracked-path limit")
    tracked_set = set(tracked)
    changed = set(context.changed_files)
    symbols: set[str] = set()
    total_bytes = 0
    scanned_sources = 0
    skipped_by_limits = 0
    for path in sorted(changed & tracked_set):
        size = int(_git(repository, "cat-file", "-s", f"{context.head_sha}:{path}"))
        if size > _DISCOVERY_MAX_SOURCE_BYTES or total_bytes + size > _DISCOVERY_MAX_TOTAL_BYTES:
            skipped_by_limits += 1
            continue
        source = _git(repository, "show", f"{context.head_sha}:{path}")
        total_bytes += len(source)
        scanned_sources += 1
        symbols.update(_defined_symbols(path, source))
        if len(symbols) >= _DISCOVERY_MAX_SYMBOLS:
            break
    ordered_symbols = tuple(sorted(symbols, key=lambda name: (-len(name), name))[:_DISCOVERY_MAX_SYMBOLS])

    grep_paths: set[str] = set()
    if ordered_symbols:
        command = ["git", "-C", str(repository), "grep", "-I", "-F", "-w", "-l", "-z"]
        for symbol in ordered_symbols:
            command.extend(("-e", symbol))
        command.extend((context.head_sha, "--"))
        result = subprocess.run(command, capture_output=True, check=False)
        if result.returncode not in {0, 1}:
            raise ValueError("pinned Git symbol search failed")
        if len(result.stdout) > _DISCOVERY_MAX_TOTAL_BYTES:
            raise ValueError("neighbor scan result exceeds the output limit")
        try:
            grep_paths = {
                part.decode("utf-8").split(":", 1)[-1]
                for part in result.stdout.split(b"\0")
                if part
            }
        except UnicodeDecodeError as exc:
            raise ValueError("neighbor paths must be valid UTF-8") from exc

    changed_path_tokens = set().union(*(_path_tokens(path) for path in changed))
    test_paths = {
        path
        for path in tracked
        if _TEST_PATH.search(path) and (_path_tokens(path) & changed_path_tokens)
    }
    candidates = []
    candidate_paths = sorted(
        (grep_paths | test_paths) - changed,
        key=lambda path: (
            0 if "test" in _neighbor_categories(path) else
            1 if "contract" in _neighbor_categories(path) else 2,
            0 if path in grep_paths else 1,
            path,
        ),
    )[:min(_DISCOVERY_MAX_CANDIDATES * 4, max_candidates * 4)]
    for path in candidate_paths:
        if path not in tracked_set:
            continue
        size = int(_git(repository, "cat-file", "-s", f"{context.head_sha}:{path}"))
        if size > _DISCOVERY_MAX_SOURCE_BYTES or total_bytes + size > _DISCOVERY_MAX_TOTAL_BYTES:
            skipped_by_limits += 1
            continue
        source = _git(repository, "show", f"{context.head_sha}:{path}")
        total_bytes += len(source)
        scanned_sources += 1
        try:
            text = source.decode("utf-8")
        except UnicodeDecodeError:
            continue
        matches_by_symbol: dict[str, list[int]] = {}
        if ordered_symbols:
            alternatives = "|".join(re.escape(symbol) for symbol in ordered_symbols)
            expression = re.compile(
                rf"(?<![A-Za-z0-9_$])(?P<symbol>{alternatives})(?![A-Za-z0-9_$])"
            )
            for line_number, line in enumerate(text.splitlines(), 1):
                for match in expression.finditer(line):
                    lines = matches_by_symbol.setdefault(match.group("symbol"), [])
                    if (not lines or lines[-1] != line_number) and len(lines) < 9:
                        lines.append(line_number)
        matches = [
            {"symbol": symbol, "lines": lines[:8], "more_lines": len(lines) > 8}
            for symbol, lines in sorted(matches_by_symbol.items())
        ]
        categories = _neighbor_categories(path)
        reasons = []
        if matches:
            reasons.append("symbol_reference")
        if "test" in categories and path in test_paths:
            reasons.append("test_path_convention")
        if "contract" in categories and matches:
            reasons.append("contract_path_and_symbol_reference")
        if not reasons:
            continue
        candidates.append(
            {
                "path": path,
                "categories": list(categories),
                "matched_symbols": matches,
                "reasons": reasons,
                "size_bytes": len(source),
                "sha256": hashlib.sha256(source).hexdigest(),
            }
        )

    candidates.sort(
        key=lambda item: (
            0 if "test" in item["categories"] else 1 if "contract" in item["categories"] else 2,
            item["path"],
        )
    )
    candidates = candidates[:max_candidates]
    used_refs = set(context.evidence_refs)
    records = []
    next_id = 1
    for candidate in candidates:
        while f"N{next_id:04d}" in used_refs:
            next_id += 1
        record = EvidenceRecord(
            evidence_ref=f"N{next_id:04d}",
            kind="source",
            sha256=candidate["sha256"],
            revision=context.head_sha,
            path=candidate["path"],
        )
        used_refs.add(record.evidence_ref)
        next_id += 1
        records.append({**candidate, "record": record.model_dump(mode="json")})

    return {
        "schema_version": 1,
        "method": "pinned_git_symbol_scan",
        "base_sha": context.base_sha,
        "head_sha": context.head_sha,
        "diff_sha256": context.diff_sha256,
        "changed_files": list(context.changed_files),
        "defined_symbols": list(ordered_symbols),
        "candidate_records": records,
        "candidate_limit": max_candidates,
        "candidate_scan_limit": min(_DISCOVERY_MAX_CANDIDATES * 4, max_candidates * 4),
        "source_files_scanned": scanned_sources,
        "sources_skipped_by_scan_limits": skipped_by_limits,
        "exhaustive": False,
        "notes": [
            "These are host-review suggestions, not an evidence-completeness claim.",
            "Review candidates and reconcile callers/contracts with CodeGraph before binding them.",
            "Add accepted record objects and refs to the full registry before building the Evidence Packet.",
        ],
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
    discover = subparsers.add_parser(
        "discover", help="Suggest pinned caller, contract and test evidence around changed symbols"
    )
    discover.add_argument("--max-candidates", default=50, type=int)
    pack = subparsers.add_parser("pack", help="Build exact profile-selectable context from pinned refs")
    pack.add_argument("--plan", type=Path, help="Explicit host plan: optional_profiles, priorities, external_content")
    pack.add_argument("--bound-context", required=True, type=Path)
    for command in (build, verify, pack, discover):
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
    if args.command == "discover":
        payload = discover_neighbor_evidence(
            context, args.repository, max_candidates=args.max_candidates
        )
        output = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        if args.output:
            args.output.write_text(output, encoding="utf-8")
        else:
            print(output, end="")
        return 0
    if args.command == "pack":
        from qa_orchestrator.context_pack import (
            MAX_CONTEXT_PACKET_BYTES,
            build_context_packet,
        )

        if args.output is None or args.output.resolve() == args.bound_context.resolve():
            raise ValueError("pack requires distinct output and bound-context paths")
        plan = json.loads(args.plan.read_text()) if args.plan else {}
        packet = build_context_packet(context, args.repository, **plan)
        data = packet.model_dump_json().encode("utf-8")
        if len(data) > MAX_CONTEXT_PACKET_BYTES:
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
