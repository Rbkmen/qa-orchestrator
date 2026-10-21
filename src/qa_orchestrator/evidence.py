"""Host-local evidence construction and verification against pinned Git objects."""

import argparse
import ast
import hashlib
import json
import re
import shutil
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath

from qa_orchestrator.contracts import (
    EvidenceRecord,
    FinalReviewReport,
    ImpactCoverageLedger,
    ProfileReviewReport,
    ReviewContext,
    ReviewCriterionCoverage,
    ReviewDiffHunk,
    ReviewFindingCandidate,
    impact_candidate_set_sha256,
    review_changed_paths_sha256,
    review_diff_hunk_inventory_sha256,
)

_DISCOVERY_MAX_SYMBOLS = 128
_DISCOVERY_MAX_TRACKED_PATHS = 200_000
_DISCOVERY_MAX_SOURCE_BYTES = 4_000_000
_DISCOVERY_MAX_TOTAL_BYTES = 64_000_000
_DISCOVERY_MAX_CANDIDATES = 200
_DISCOVERY_MAX_DIFF_HUNKS = 500
_DISCOVERY_MAX_CANDIDATE_SCAN_PATHS = 200
_DISCOVERY_MAX_SYMBOL_SEARCH_PATHS = 32
_DISCOVERY_MAX_SYMBOL_SEARCH_BYTES = 16_000_000
_DISCOVERY_MAX_MODULE_SEARCH_TERMS = 48
_DISCOVERY_MAX_MODULE_SEARCH_BYTES = 32_000_000
_DISCOVERY_SYMBOL_SEARCH_BATCH_SIZE = 32
_DISCOVERY_MAX_OMISSION_EXAMPLES = 20
_CODEGRAPH_MAX_SYMBOL_QUERIES = 24
_CODEGRAPH_MAX_RELATION_CANDIDATES = 100
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
_API_PATH = re.compile(
    r"(^|/)(api|routes?|controllers?|endpoints?|graphql|openapi|proto)(/|$)", re.IGNORECASE
)
_DATABASE_PATH = re.compile(
    r"(^|/)(models?|migrations?|database|db|sql|prisma)(/|$)|(^|/)(schema\.(?:rb|sql))$",
    re.IGNORECASE,
)
_EVENT_PATH = re.compile(
    r"(^|/)(events?|pubsub|topics?|streams?|messages?)(/|$)", re.IGNORECASE
)
_BACKGROUND_PATH = re.compile(
    r"(^|/)(jobs?|workers?|queues?|cron|schedulers?|background)(/|$)", re.IGNORECASE
)
_CONFIG_PATH = re.compile(
    r"(^|/)(configs?|configuration|settings?|feature.?flags?|flags?)(/|$)|(^|/)\.env(?:\.|$)",
    re.IGNORECASE,
)
_AUTHORIZATION_PATH = re.compile(
    r"(^|/)(auth|authorization|permissions?|policies|rbac|roles?)(/|$)", re.IGNORECASE
)
_DEPLOYMENT_PATH = re.compile(
    r"(^|/)(deploy|deployments?|helm|charts?|k8s|kubernetes|terraform|infrastructure|infra)(/|$)"
    r"|(^|/)(?:Dockerfile(?:\..*)?|docker-compose(?:\.[^.]+)?\.ya?ml)$",
    re.IGNORECASE,
)
_SERVICE_BOUNDARY_PATH = re.compile(
    r"(^|/)(services?|clients?|integrations?|adapters?)(/|$)", re.IGNORECASE
)
_SYSTEM_BOUNDARY_CATEGORIES = frozenset(
    {
        "api", "authorization", "config", "contract", "database", "deployment",
        "event", "job", "service_boundary",
    }
)
_INPUT_SCOPE_POLICY_VERSION = 1
_SCOPE_REQUIRED_INPUT_KINDS = {
    "api": ("api_contract", "api_consumer_map"),
    "contract": ("api_contract", "api_consumer_map"),
    "database": ("database_schema", "database_consumer_map"),
    "event": ("event_contract", "event_consumer_map"),
    "job": ("job_registration", "trigger_consumer_map"),
    "config": ("configuration_contract", "flag_consumer_map"),
    "authorization": ("authorization_policy", "authorization_consumer_map"),
    "deployment": ("deployment_manifest", "service_dependency_map"),
    "service_boundary": ("service_dependency_map",),
}
_TS_IMPORT = re.compile(
    r"\bimport\s+(?P<bindings>[^;]*?)\s+from\s*['\"](?P<module>[^'\"]+)['\"]",
    re.DOTALL,
)
_JS_TEST_MANIFESTS = {
    "package.json",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "yarn.lock",
}
_TEST_DEPENDENCY_MANIFESTS = {
    ".py": {
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "tox.ini",
        "pytest.ini",
        "Pipfile",
        "Pipfile.lock",
    },
    ".js": _JS_TEST_MANIFESTS,
    ".jsx": _JS_TEST_MANIFESTS,
    ".ts": _JS_TEST_MANIFESTS,
    ".tsx": _JS_TEST_MANIFESTS,
    ".go": {"go.mod"},
    ".rb": {"Gemfile", "Gemfile.lock"},
    ".rs": {"Cargo.toml", "Cargo.lock"},
    ".java": {"pom.xml", "build.gradle", "build.gradle.kts"},
}


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


def _patch_path(raw_path: bytes) -> str | None:
    """Decode a unified-diff path header, including Git's C-style quoting."""
    raw_path = raw_path.rstrip(b"\n")
    if raw_path.startswith(b'"'):
        if not raw_path.endswith(b'"'):
            raise ValueError("malformed quoted Git diff path")
        encoded = raw_path[1:-1]
        decoded = bytearray()
        index = 0
        escapes = {
            ord('"'): ord('"'),
            ord("\\"): ord("\\"),
            ord("a"): 7,
            ord("b"): 8,
            ord("t"): 9,
            ord("n"): 10,
            ord("v"): 11,
            ord("f"): 12,
            ord("r"): 13,
        }
        while index < len(encoded):
            byte = encoded[index]
            if byte != ord("\\"):
                decoded.append(byte)
                index += 1
                continue
            index += 1
            if index >= len(encoded):
                raise ValueError("malformed Git diff path escape")
            if (
                encoded[index] in b"01234567"
                and index + 2 < len(encoded)
                and encoded[index + 1] in b"01234567"
                and encoded[index + 2] in b"01234567"
            ):
                decoded.append(int(encoded[index : index + 3], 8))
                index += 3
                continue
            escaped = escapes.get(encoded[index])
            if escaped is None:
                raise ValueError("unsupported Git diff path escape")
            decoded.append(escaped)
            index += 1
        raw_path = bytes(decoded)
    if raw_path == b"/dev/null":
        return None
    if raw_path.startswith((b"a/", b"b/")):
        raw_path = raw_path[2:]
    else:
        raise ValueError("Git diff path is missing its source prefix")
    try:
        return raw_path.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Git diff path is not valid UTF-8") from exc


_DIFF_HUNK_HEADER = re.compile(
    rb"^@@ -(?P<old_start>[0-9]+)(?:,(?P<old_count>[0-9]+))? "
    rb"\+(?P<new_start>[0-9]+)(?:,(?P<new_count>[0-9]+))? @@"
)


def _parse_diff_hunks(
    context: ReviewContext,
    diff: bytes,
) -> tuple[ReviewDiffHunk, ...]:
    path_indexes = {path: index for index, path in enumerate(context.changed_files)}
    lines = diff.splitlines(keepends=True)
    hunks = []
    old_path = None
    new_path = None
    active_path = None
    in_file_header = False
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith(b"diff --git "):
            old_path = new_path = active_path = None
            in_file_header = True
            index += 1
            continue
        if in_file_header and line.startswith(b"--- "):
            old_path = _patch_path(line[4:])
            index += 1
            continue
        if in_file_header and line.startswith(b"+++ "):
            new_path = _patch_path(line[4:])
            active_path = new_path or old_path
            index += 1
            continue
        if line.startswith(b"@@ "):
            in_file_header = False
            if active_path is None or active_path not in path_indexes:
                raise ValueError("diff hunk is not bound to a changed path")
            match = _DIFF_HUNK_HEADER.match(line.rstrip(b"\n"))
            if match is None:
                raise ValueError("malformed unified-diff hunk header")
            end = index + 1
            while end < len(lines) and not lines[end].startswith(
                (b"@@ ", b"diff --git ")
            ):
                end += 1
            hunk_bytes = b"".join(lines[index:end])
            if len(hunks) >= _DISCOVERY_MAX_DIFF_HUNKS:
                raise OverflowError("diff hunk inventory exceeds its configured limit")
            old_start = int(match.group("old_start"))
            old_count = int(match.group("old_count") or b"1")
            new_start = int(match.group("new_start"))
            new_count = int(match.group("new_count") or b"1")
            hunk = ReviewDiffHunk(
                index=len(hunks),
                changed_path_index=path_indexes[active_path],
                old_start=old_start,
                old_count=old_count,
                new_start=new_start,
                new_count=new_count,
                header=(
                    f"@@ -{old_start}"
                    + (f",{old_count}" if match.group("old_count") is not None else "")
                    + f" +{new_start}"
                    + (f",{new_count}" if match.group("new_count") is not None else "")
                    + " @@"
                ),
                sha256=hashlib.sha256(hunk_bytes).hexdigest(),
            )
            hunks.append(hunk)
            index = end
            continue
        index += 1
    return tuple(hunks)


def _diff_hunk_inventory(context: ReviewContext, diff: bytes) -> dict[str, object]:
    try:
        hunks = _parse_diff_hunks(context, diff)
    except OverflowError:
        return {
            "status": "partial",
            "max_hunks": _DISCOVERY_MAX_DIFF_HUNKS,
            "count_at_least": _DISCOVERY_MAX_DIFF_HUNKS + 1,
            "hunks": [],
            "inventory_sha256": None,
            "reason": "diff_hunk_limit_reached",
        }
    except (UnicodeDecodeError, ValueError):
        return {
            "status": "partial",
            "max_hunks": _DISCOVERY_MAX_DIFF_HUNKS,
            "count_at_least": None,
            "hunks": [],
            "inventory_sha256": None,
            "reason": "diff_hunk_parse_failed",
        }
    return {
        "status": "complete",
        "max_hunks": _DISCOVERY_MAX_DIFF_HUNKS,
        "count": len(hunks),
        "hunks": [hunk.model_dump(mode="json") for hunk in hunks],
        "inventory_sha256": review_diff_hunk_inventory_sha256(
            context.diff_sha256, hunks
        ),
    }


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
        entries.append(
            entry.model_copy(
                update={
                    "sha256": hashlib.sha256(data).hexdigest(),
                    **(
                        {"source_text_available": _is_text_source(data)}
                        if entry.kind == "source"
                        else {}
                    ),
                }
            )
        )
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
                # Records written before the text-availability flag are legacy
                # metadata. Preserve their historical citation behavior; new
                # registrations always write an explicit True/False value.
                and registry[ref].source_text_available is not False
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
        if entry.kind in {"diff", "requirement", "claim"}
    }
    if mandatory - set(selection.included_refs):
        raise ValueError("context selection omits mandatory evidence")
    cited = {ref for f in report.finding_candidates for ref in f.evidence_refs}
    cited.update(ref for note in (*report.coverage_gaps, *report.unverified) for ref in note.evidence_refs)
    cited.update(item.evidence_ref for item in report.evidence_map)
    if cited - set(selection.included_refs):
        raise ValueError("profile report cites evidence omitted from its context")


def profile_input_receipt_verified(context: ReviewContext, report: ProfileReviewReport) -> bool:
    """Check that the final attempt records the exact selected packet and a completed stdin write."""
    selection = report.context_selection
    metrics = report.execution_metrics
    if selection is None or metrics is None or not metrics.attempts:
        return False
    try:
        validate_context_selection(context, report)
    except ValueError:
        return False

    attempt = metrics.attempts[-1]
    if (
        attempt.profile != report.profile
        or attempt.outcome != "completed"
        or attempt.attempt != metrics.attempt_count
        or sum(item.outcome == "completed" for item in metrics.attempts) != 1
        or attempt.review_head_sha != context.head_sha
        or attempt.review_diff_sha256 != context.diff_sha256
        or attempt.prompt_write_status != "written"
        or attempt.submitted_prompt_sha256 is None
        or attempt.source_packet_sha256 != context.evidence_packet_sha256
        or attempt.source_packet_sha256 != selection.source_packet_sha256
        or attempt.selected_packet_sha256 != selection.selected_packet_sha256
        or attempt.included_evidence_refs != selection.included_refs
        or attempt.omitted_evidence_refs != selection.omitted_refs
    ):
        return False

    included_refs = set(attempt.included_evidence_refs)
    expected_source_paths = tuple(
        sorted(
            {
                entry.path
                for entry in context.evidence_registry
                if entry.kind == "source"
                and entry.revision == context.head_sha
                and entry.source_text_available is True
                and entry.path in context.changed_files
                and entry.evidence_ref in included_refs
            }
        )
    )
    return attempt.included_source_paths == expected_source_paths


def input_manifest_completeness(context: ReviewContext | None) -> dict[str, object]:
    """Check host inputs against source kinds required by the changed-path scope."""
    manifest = context.input_manifest if context is not None else None
    sources = manifest.sources if manifest is not None else ()
    required_sources = tuple(source for source in sources if source.required)
    optional_sources = tuple(source for source in sources if not source.required)
    scope_categories = sorted(
        {
            category
            for path in (context.changed_files if context is not None else ())
            for category in _neighbor_categories(path)
            if category in _SCOPE_REQUIRED_INPUT_KINDS
        }
    )
    scope_required_source_kinds = sorted(
        {
            source_kind
            for category in scope_categories
            for source_kind in _SCOPE_REQUIRED_INPUT_KINDS[category]
        }
    )
    declared_required_kinds = {source.kind for source in required_sources}
    missing_scope_source_kinds = sorted(
        set(scope_required_source_kinds) - declared_required_kinds
    )

    def incomplete_reasons(source) -> tuple[str, ...]:
        reasons = []
        if source.retrieval_status != "complete":
            reasons.append(f"retrieval {source.retrieval_status}")
        if source.pagination_status not in {"complete", "not_applicable"}:
            reasons.append(f"pagination {source.pagination_status}")
        if source.extraction_status != "complete":
            reasons.append(f"extraction {source.extraction_status}")
        return tuple(reasons)

    incomplete_sources = [
        {
            "source_id": source.source_id,
            "required": source.required,
            "retrieval_status": source.retrieval_status,
            "pagination_status": source.pagination_status,
            "extraction_status": source.extraction_status,
            "reasons": incomplete_reasons(source),
        }
        for source in sources
        if incomplete_reasons(source)
    ]
    required_incomplete = tuple(
        source for source in required_sources if incomplete_reasons(source)
    )

    registered_criteria = (
        {
            record.evidence_ref
            for record in context.evidence_registry
            if record.kind in {"requirement", "claim"}
        }
        if context is not None
        else set()
    )
    declared_criteria = {
        criterion_ref
        for source in sources
        for criterion_ref in source.criterion_refs
    }
    unlisted_criteria = sorted(registered_criteria - declared_criteria)
    required_criteria = {
        criterion_ref
        for source in required_sources
        for criterion_ref in source.criterion_refs
    }
    missing_criterion_refs = sorted(declared_criteria - registered_criteria)
    missing_required_criterion_refs = sorted(required_criteria - registered_criteria)
    source_inventory_status = (
        manifest.source_inventory_status if manifest is not None and manifest.schema_version == 2 else None
    )
    source_conflict_status = (
        manifest.source_conflict_status if manifest is not None and manifest.schema_version == 2 else None
    )
    source_conflicts = (
        [conflict.model_dump(mode="json") for conflict in manifest.source_conflicts]
        if manifest is not None and manifest.schema_version == 2
        else []
    )
    assessment_reasons = []
    if manifest is not None and manifest.schema_version == 2:
        if source_inventory_status != "complete":
            assessment_reasons.append(f"source inventory is {source_inventory_status}")
        if source_conflict_status == "unknown":
            assessment_reasons.append("description conflict assessment is unknown")
        elif source_conflict_status == "unresolved":
            unresolved_ids = [
                conflict["conflict_id"]
                for conflict in source_conflicts
                if conflict["disposition"] == "unresolved"
            ]
            assessment_reasons.append(
                "description conflicts remain unresolved: " + ", ".join(unresolved_ids)
            )

    if context is None:
        status = "unknown"
        status_reason = "review context is unavailable"
    elif manifest is None:
        if missing_scope_source_kinds:
            status = "partial"
            status_reason = (
                "input manifest is missing required sources for changed path scope: "
                + ", ".join(missing_scope_source_kinds)
            )
        else:
            status = "unknown"
            status_reason = "input manifest was not provided"
    elif missing_scope_source_kinds:
        status = "partial"
        status_reason = (
            "changed path scope requires undeclared sources: "
            + ", ".join(missing_scope_source_kinds)
        )
    elif not required_sources:
        status = "unknown"
        status_reason = "no required source was declared"
    elif required_incomplete or missing_required_criterion_refs or unlisted_criteria:
        status = "partial"
        status_reason = "one or more required sources or criteria are incomplete or unlisted"
    elif assessment_reasons:
        status = (
            "partial"
            if source_inventory_status == "partial" or source_conflict_status == "unresolved"
            else "unknown"
        )
        status_reason = "; ".join(assessment_reasons)
    else:
        status = "complete"
        status_reason = "all required sources and declared criteria are accounted for"

    return {
        "status": status,
        "status_reason": status_reason,
        "scope_policy_version": _INPUT_SCOPE_POLICY_VERSION,
        "scope_categories": scope_categories,
        "scope_required_source_kinds": scope_required_source_kinds,
        "missing_scope_source_kinds": missing_scope_source_kinds,
        "source_inventory_status": source_inventory_status,
        "source_conflict_status": source_conflict_status,
        "source_conflicts": source_conflicts,
        "source_counts": {
            "declared": len(sources),
            "required": len(required_sources),
            "optional": len(optional_sources),
            "complete_required": len(required_sources) - len(required_incomplete),
            "incomplete_required": len(required_incomplete),
            "incomplete_optional": sum(
                bool(incomplete_reasons(source)) for source in optional_sources
            ),
        },
        "incomplete_sources": incomplete_sources,
        "missing_criterion_refs": missing_criterion_refs,
        "missing_required_criterion_refs": missing_required_criterion_refs,
        "unlisted_criterion_refs": unlisted_criteria,
    }


def impact_coverage_status(ledger: ImpactCoverageLedger | None) -> str:
    """Return closure only for candidates in the recorded bounded discovery."""
    if ledger is None:
        return "unknown"
    if (
        ledger.discovery_state == "partial"
        or ledger.omissions
        or any(limit.reached or limit.uninspected for limit in ledger.limits)
        or any(
            candidate.disposition in {"unresolved", "not_checked"}
            for candidate in ledger.candidates
        )
    ):
        return "partial"
    if ledger.discovery_state == "unknown":
        return "unknown"
    return "closed"


def validate_impact_coverage_context(
    report: FinalReviewReport,
    context: ReviewContext,
    *,
    require_ledger: bool = False,
) -> None:
    """Validate that an impact ledger belongs to this exact bounded review."""
    ledger = report.impact_coverage
    if ledger is None:
        if require_ledger:
            raise ValueError("impact coverage ledger is required")
        return
    if (
        ledger.head_sha != context.head_sha
        or ledger.changed_paths_sha256 != review_changed_paths_sha256(context.changed_files)
        or context.neighbor_discovery_sha256 is None
        or ledger.discovery_sha256 != context.neighbor_discovery_sha256
    ):
        raise ValueError("impact coverage ledger does not match the bound review")
    if (
        context.neighbor_discovery_candidate_count is None
        or context.neighbor_discovery_candidate_set_sha256 is None
    ):
        raise ValueError("impact coverage requires a bound discovery candidate set")
    if (
        len(ledger.candidates) != context.neighbor_discovery_candidate_count
        or impact_candidate_set_sha256(
            tuple(
                (candidate.candidate_id, candidate.relation, candidate.locator)
                for candidate in ledger.candidates
            )
        )
        != context.neighbor_discovery_candidate_set_sha256
    ):
        raise ValueError("impact coverage candidates do not exactly match discovery")
    impact_refs = {
        ref
        for candidate in ledger.candidates
        for ref in candidate.evidence_refs
    }
    if impact_refs - set(context.evidence_refs):
        raise ValueError("impact coverage cites refs outside the bound context")


def review_completion_gaps(
    context: ReviewContext,
    report: FinalReviewReport,
    *,
    allow_legacy_missing_hunk_ledger: bool = False,
) -> tuple[str, ...]:
    """Describe why a bound review cannot be finalized as completed."""
    gaps = []
    input_status = input_manifest_completeness(context)
    if input_status["status"] != "complete":
        gaps.append(
            f"input manifest is {input_status['status']}: {input_status['status_reason']}"
        )
        incomplete_required = [
            source for source in input_status["incomplete_sources"] if source["required"]
        ]
        if incomplete_required:
            examples = [
                f"{source['source_id']} ({', '.join(source['reasons'])})"
                for source in incomplete_required[:5]
            ]
            if len(incomplete_required) > len(examples):
                examples.append(f"{len(incomplete_required) - len(examples)} more")
            gaps.append("incomplete required sources: " + ", ".join(examples))
        missing_criteria = input_status["missing_required_criterion_refs"]
        if missing_criteria:
            examples = list(missing_criteria[:5])
            if len(missing_criteria) > len(examples):
                examples.append(f"{len(missing_criteria) - len(examples)} more")
            gaps.append("required criterion refs missing from registry: " + ", ".join(examples))
        unlisted_criteria = input_status["unlisted_criterion_refs"]
        if unlisted_criteria:
            examples = list(unlisted_criteria[:5])
            if len(unlisted_criteria) > len(examples):
                examples.append(f"{len(unlisted_criteria) - len(examples)} more")
            gaps.append("registered criterion refs missing from manifest: " + ", ".join(examples))
    missing_scope_source_kinds = input_status["missing_scope_source_kinds"]
    if missing_scope_source_kinds:
        gaps.append(
            "required scope source kinds missing from manifest: "
            + ", ".join(missing_scope_source_kinds)
        )
    try:
        validate_impact_coverage_context(report, context, require_ledger=True)
    except ValueError as exc:
        gaps.append(str(exc))
    if report.impact_coverage is not None:
        status = impact_coverage_status(report.impact_coverage)
        if status != "closed":
            gaps.append(f"impact coverage status is {status}")
    coverage = report.coverage_ledger
    if not (allow_legacy_missing_hunk_ledger and coverage is None):
        if context.diff_hunk_inventory_sha256 is None:
            gaps.append("complete diff hunk inventory is not bound to the review")
        elif (
            coverage is None
            or coverage.diff_hunk_inventory_sha256 != context.diff_hunk_inventory_sha256
            or coverage.diff_hunk_count != len(context.diff_hunks)
        ):
            gaps.append("diff hunk coverage ledger does not match the bound inventory")
        elif coverage.unreviewed_diff_hunk_indexes:
            examples = ", ".join(
                str(index) for index in coverage.unreviewed_diff_hunk_indexes[:10]
            )
            if len(coverage.unreviewed_diff_hunk_indexes) > 10:
                examples += ", ..."
            gaps.append("unreviewed diff hunk indexes: " + examples)
    return tuple(gaps)


def complete_criterion_coverage(
    context: ReviewContext,
    report: FinalReviewReport,
) -> FinalReviewReport:
    """Keep every registered requirement or MR claim visible in the final report."""
    criteria = {
        entry.evidence_ref: entry.kind
        for entry in context.evidence_registry
        if entry.kind in {"requirement", "claim"}
    }
    if set(criteria) - set(context.evidence_refs):
        raise ValueError("registered criteria must belong to the bound evidence packet")
    if len(criteria) > 100:
        raise ValueError("bound review has more criteria than the final report can represent")

    supplied = {item.criterion_ref: item for item in report.criterion_coverage}
    unknown = set(supplied) - set(criteria)
    if unknown:
        raise ValueError("criterion coverage cites an unregistered requirement or MR claim")

    registry = {entry.evidence_ref: entry for entry in context.evidence_registry}
    packet_refs = set(context.evidence_refs)
    for item in report.criterion_coverage:
        expected_kind = "requirement" if item.criterion_kind == "requirement" else "claim"
        if criteria[item.criterion_ref] != expected_kind:
            raise ValueError("criterion coverage kind does not match its bound evidence")
        supporting_refs = set(item.implementation_refs) | set(item.test_refs)
        if supporting_refs - packet_refs:
            raise ValueError("criterion coverage cites evidence outside the bound packet")
        if supporting_refs - set(registry):
            raise ValueError("criterion coverage evidence must have registered locators")
        for ref in item.implementation_refs:
            evidence = registry.get(ref)
            if evidence is not None and evidence.kind not in {"source", "diff"}:
                raise ValueError("implementation evidence must reference source or diff evidence")
        for ref in item.test_refs:
            evidence = registry.get(ref)
            if evidence is not None and evidence.kind not in {"source", "runtime"}:
                raise ValueError("test evidence must reference source or runtime evidence")

    missing = set(criteria) - set(supplied)
    if not missing:
        return report

    generated = [
        ReviewCriterionCoverage(
            criterion_ref=ref,
            criterion_kind="requirement" if criteria[ref] == "requirement" else "mr_claim",
            outcome="unverified",
            rationale="No explicit assessment was submitted; this criterion remains unverified.",
        )
        for ref in sorted(missing)
    ]
    return FinalReviewReport.model_validate(
        {
            **report.model_dump(),
            "criterion_coverage": (*report.criterion_coverage, *generated),
        }
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
    verified_entries = []
    for entry in context.evidence_registry:
        if entry.kind == "source":
            data = _source_bytes(entry, repository)
            source_text_available = _is_text_source(data)
            if (
                entry.source_text_available is not None
                and entry.source_text_available != source_text_available
            ):
                raise ValueError(f"source text availability metadata mismatch: {entry.evidence_ref}")
            verified_entries.append(
                entry.model_copy(update={"source_text_available": source_text_available})
            )
        elif entry.kind == "diff":
            data = diff
            verified_entries.append(entry)
        else:
            unverified.append(entry.evidence_ref)
            verified_entries.append(entry)
            continue
        if hashlib.sha256(data).hexdigest() != entry.sha256:
            raise ValueError(f"evidence content hash mismatch: {entry.evidence_ref}")
        verified.append(entry.evidence_ref)
    if (
        packet is not None
        and hashlib.sha256(packet.read_bytes()).hexdigest() != context.evidence_packet_sha256
    ):
        raise ValueError("Evidence Packet hash mismatch")
    verified_context = context.model_copy(update={"evidence_registry": tuple(verified_entries)})
    validate_registered_locations(verified_context, candidates)
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


def _local_module_exists(module: str, tracked_paths: set[str]) -> bool:
    module_path = module.replace(".", "/").strip("/")
    if not module_path:
        return False
    return (
        f"{module_path}.py" in tracked_paths
        or f"{module_path}.pyi" in tracked_paths
        or f"{module_path}/__init__.py" in tracked_paths
    )


def _relative_python_module(path: str, module: str | None, level: int) -> str:
    if not level:
        return (module or "").replace(".", "/")
    parent = PurePosixPath(path).parent.parts
    retained = max(0, len(parent) - level + 1)
    module_parts = tuple(module.split(".")) if module else ()
    parts = parent[:retained] + module_parts
    return "/".join(part for part in parts if part)


def _tracked_module_paths(module: str, tracked_paths: set[str]) -> set[str]:
    module_path = module.replace(".", "/").strip("/")
    if not module_path:
        return set()
    return {
        candidate
        for candidate in (
            f"{module_path}.py",
            f"{module_path}.pyi",
            f"{module_path}/__init__.py",
        )
        if candidate in tracked_paths
    }


def _normalized_relative_module_path(path: str, module: str) -> str:
    parts = list(PurePosixPath(path).parent.parts)
    for part in module.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def _tracked_typescript_module_paths(
    path: str, module: str, tracked_paths: set[str]
) -> set[str]:
    if module.startswith("."):
        target = _normalized_relative_module_path(path, module)
    elif module.startswith(("@/", "~/")):
        target = module[2:]
        if not any(f"{target}{suffix}" in tracked_paths for suffix in (".ts", ".tsx", ".js", ".jsx")):
            target = f"src/{target}"
    elif module.startswith("src/"):
        target = module
    else:
        return set()

    variants = [target]
    suffixes = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts", ".json")
    if PurePosixPath(target).suffix.lower() in suffixes:
        suffix = PurePosixPath(target).suffix
        if suffix in {".js", ".jsx", ".mjs", ".cjs"}:
            stem = target[: -len(suffix)]
            variants.extend(f"{stem}{replacement}" for replacement in (".ts", ".tsx", ".mts", ".cts"))
    else:
        variants.extend(f"{target}{suffix}" for suffix in suffixes)
    variants.extend(f"{target}/index{suffix}" for suffix in suffixes if suffix != ".json")
    return {candidate for candidate in variants if candidate in tracked_paths}


def _referenced_module_paths(path: str, source: bytes, tracked_paths: set[str]) -> set[str]:
    """Resolve static local Python or JavaScript/TypeScript imports."""
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError:
        return set()
    if PurePosixPath(path).suffix.lower() == ".py":
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return set()
        paths: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = _relative_python_module(path, node.module, node.level)
                paths.update(_tracked_module_paths(module, tracked_paths))
                for alias in node.names:
                    paths.update(_tracked_module_paths(f"{module}/{alias.name}", tracked_paths))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    paths.update(_tracked_module_paths(alias.name, tracked_paths))
        return paths
    if PurePosixPath(path).suffix.lower() in {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}:
        paths = set()
        for match in _TS_IMPORT.finditer(text):
            paths.update(
                _tracked_typescript_module_paths(path, match.group("module"), tracked_paths)
            )
        return paths
    return set()


def _local_dotted_symbol(target: str, tracked_paths: set[str]) -> str | None:
    parts = target.replace(":", ".").split(".")
    for index in range(len(parts) - 1, 0, -1):
        if _local_module_exists(".".join(parts[:index]), tracked_paths):
            symbol = parts[index]
            return symbol if _IDENTIFIER.fullmatch(symbol) else None
    return None


def _attribute_chain(expression: ast.expr) -> tuple[str, ...]:
    parts = []
    while isinstance(expression, ast.Attribute):
        parts.append(expression.attr)
        expression = expression.value
    if isinstance(expression, ast.Name):
        return tuple(reversed((*parts, expression.id)))
    return ()


def _referenced_symbols(path: str, source: bytes, tracked_paths: set[str]) -> tuple[str, ...]:
    """Return locally imported or mocked symbols referenced by changed files."""
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError:
        return ()

    names: set[str] = set()
    if path.endswith(".py"):
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return ()
        module_aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = _relative_python_module(path, node.module, node.level)
                if module and _local_module_exists(module, tracked_paths):
                    for alias in node.names:
                        if alias.name == "*":
                            continue
                        names.add(alias.name)
                        child = f"{module}.{alias.name}"
                        if _local_module_exists(child, tracked_paths):
                            module_aliases[alias.asname or alias.name] = child
                else:
                    for alias in node.names:
                        child = f"{module}.{alias.name}" if module else alias.name
                        if _local_module_exists(child, tracked_paths):
                            names.add(alias.name)
                            module_aliases[alias.asname or alias.name] = child
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if _local_module_exists(alias.name, tracked_paths):
                        binding = alias.asname or alias.name.split(".", 1)[0]
                        if alias.asname or "." not in alias.name:
                            names.add(binding)
                        module_aliases[binding] = alias.name if alias.asname else binding

            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute):
                chain = _attribute_chain(node.func)
                if chain and chain[0] in module_aliases:
                    target = ".".join((module_aliases[chain[0]], *chain[1:]))
                    symbol = _local_dotted_symbol(target, tracked_paths)
                    if symbol:
                        names.add(symbol)
            if isinstance(node.func, ast.Name):
                call_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                call_name = node.func.attr
            else:
                call_name = ""
            target_nodes = node.args[:1] if call_name == "patch" else ()
            if call_name == "setattr" and node.args:
                if isinstance(node.args[0], ast.Constant):
                    target_nodes = node.args[:1]
                elif (
                    len(node.args) > 1
                    and isinstance(node.args[0], ast.Name)
                    and node.args[0].id in module_aliases
                    and isinstance(node.args[1], ast.Constant)
                    and isinstance(node.args[1].value, str)
                ):
                    target = f"{module_aliases[node.args[0].id]}.{node.args[1].value}"
                    symbol = _local_dotted_symbol(target, tracked_paths)
                    if symbol:
                        names.add(symbol)
            for target_node in target_nodes:
                if isinstance(target_node, ast.Constant) and isinstance(target_node.value, str):
                    symbol = _local_dotted_symbol(target_node.value, tracked_paths)
                    if symbol:
                        names.add(symbol)
    elif PurePosixPath(path).suffix.lower() in {".js", ".jsx", ".ts", ".tsx"}:
        for match in _TS_IMPORT.finditer(text):
            module = match.group("module")
            if not (
                module.startswith((".", "@/", "~/", "src/"))
                or _local_module_exists(module, tracked_paths)
            ):
                continue
            names.update(
                name
                for name in re.findall(r"[A-Za-z_$][A-Za-z0-9_$]*", match.group("bindings"))
                if name not in {"as", "type"}
            )

    return tuple(
        sorted(
            (
                name for name in names
                if _IDENTIFIER.fullmatch(name) and name not in _IGNORED_IDENTIFIERS
            ),
            key=lambda name: (-len(name), name),
        )[:_DISCOVERY_MAX_SYMBOLS]
    )


def _test_dependency_manifests(changed_paths: set[str], tracked_paths: set[str]) -> set[str]:
    manifests: set[str] = set()
    changed_tests = [path for path in changed_paths if _TEST_PATH.search(path)]
    manifests_by_suffix = {suffix: set() for suffix in _TEST_DEPENDENCY_MANIFESTS}
    for candidate in tracked_paths:
        name = candidate.rsplit("/", 1)[-1]
        for suffix, names in _TEST_DEPENDENCY_MANIFESTS.items():
            if name in names or (
                suffix == ".py" and name.startswith("requirements") and name.endswith(".txt")
            ):
                manifests_by_suffix[suffix].add(candidate)
    for changed_path in changed_tests:
        suffix = PurePosixPath(changed_path).suffix.lower()
        candidates = manifests_by_suffix.get(suffix, set())
        test_parent = PurePosixPath(changed_path).parent.parts
        for candidate in candidates:
            candidate_path = PurePosixPath(candidate)
            manifest_parent = candidate_path.parent.parts
            if (
                len(manifest_parent) <= len(test_parent)
                and test_parent[: len(manifest_parent)] == manifest_parent
            ):
                manifests.add(candidate)
    return manifests


def neighbor_discovery_sha256(payload: Mapping[str, object]) -> str:
    """Fingerprint a discovery result independently of mapping insertion order."""
    if not isinstance(payload, Mapping) or any(not isinstance(key, str) for key in payload):
        raise TypeError("neighbor discovery payload must be a string-keyed mapping")
    canonical_payload = dict(payload)
    canonical_payload.pop("discovery_sha256", None)
    canonical_json = json.dumps(
        canonical_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def bind_neighbor_discovery(
    context: ReviewContext, payload: Mapping[str, object]
) -> ReviewContext:
    """Bind a verified discovery payload and its exact candidate set to a review."""
    if not isinstance(payload, Mapping):
        raise TypeError("neighbor discovery payload must be an object")
    if (
        payload.get("schema_version") != 2
        or payload.get("base_sha") != context.base_sha
        or payload.get("head_sha") != context.head_sha
        or payload.get("diff_sha256") != context.diff_sha256
        or payload.get("changed_files") != list(context.changed_files)
    ):
        raise ValueError("neighbor discovery does not match the bound review")
    if payload.get("discovery_sha256") != neighbor_discovery_sha256(payload):
        raise ValueError("neighbor discovery digest is invalid")
    candidate_records = payload.get("candidate_records")
    if not isinstance(candidate_records, list):
        raise TypeError("neighbor discovery candidate records must be a list")
    candidate_manifest = []
    for item in candidate_records:
        if not isinstance(item, Mapping) or not isinstance(item.get("record"), Mapping):
            raise TypeError("neighbor discovery candidate record is invalid")
        candidate_record = item["record"]
        candidate_id = candidate_record.get("evidence_ref")
        locator = item.get("path")
        if not isinstance(candidate_id, str) or not isinstance(locator, str):
            raise TypeError("neighbor discovery candidate identity is invalid")
        candidate_manifest.append(
            {
                "candidate_id": candidate_id,
                "relation": "discovery_candidate",
                "locator": locator,
            }
        )
    if payload.get("candidate_manifest") != candidate_manifest:
        raise ValueError("neighbor discovery candidate manifest does not match its records")
    candidate_digest = impact_candidate_set_sha256(
        tuple(
            (item["candidate_id"], item["relation"], item["locator"])
            for item in candidate_manifest
        )
    )
    if (
        type(payload.get("candidate_count")) is not int
        or payload.get("candidate_count") != len(candidate_manifest)
        or payload.get("candidate_set_sha256") != candidate_digest
    ):
        raise ValueError("neighbor discovery candidate set digest is invalid")
    inventory = payload.get("diff_hunk_inventory")
    if not isinstance(inventory, Mapping):
        raise TypeError("neighbor discovery diff hunk inventory must be an object")
    bound_hunks: tuple[ReviewDiffHunk, ...] = ()
    bound_hunk_digest = None
    if inventory.get("status") == "complete":
        raw_hunks = inventory.get("hunks")
        if not isinstance(raw_hunks, list) or type(inventory.get("count")) is not int:
            raise ValueError("complete diff hunk inventory is malformed")
        if (
            inventory.get("max_hunks") != _DISCOVERY_MAX_DIFF_HUNKS
            or inventory["count"] != len(raw_hunks)
            or len(raw_hunks) > _DISCOVERY_MAX_DIFF_HUNKS
        ):
            raise ValueError("complete diff hunk inventory has inconsistent limits")
        bound_hunks = tuple(ReviewDiffHunk.model_validate(item) for item in raw_hunks)
        bound_hunk_digest = review_diff_hunk_inventory_sha256(
            context.diff_sha256, bound_hunks
        )
        if inventory.get("inventory_sha256") != bound_hunk_digest:
            raise ValueError("diff hunk inventory digest is invalid")
    elif inventory.get("status") == "partial":
        if inventory.get("hunks") != [] or inventory.get("inventory_sha256") is not None:
            raise ValueError("partial diff hunk inventory must not claim complete hunks")
    else:
        raise ValueError("neighbor discovery diff hunk inventory status is invalid")
    return ReviewContext.model_validate(
        {
            **context.model_dump(mode="json"),
            "neighbor_discovery_sha256": payload["discovery_sha256"],
            "neighbor_discovery_candidate_count": len(candidate_manifest),
            "neighbor_discovery_candidate_set_sha256": candidate_digest,
            "diff_hunks": [hunk.model_dump(mode="json") for hunk in bound_hunks],
            "diff_hunk_inventory_sha256": bound_hunk_digest,
        }
    )


def _neighbor_categories(path: str) -> tuple[str, ...]:
    tags = []
    if _TEST_PATH.search(path):
        tags.append("test")
    if _CONTRACT_PATH.search(path):
        tags.append("contract")
    if _API_PATH.search(path):
        tags.append("api")
    if _DATABASE_PATH.search(path):
        tags.append("database")
    if _EVENT_PATH.search(path):
        tags.append("event")
    if _BACKGROUND_PATH.search(path):
        tags.append("job")
    if _CONFIG_PATH.search(path):
        tags.append("config")
    if _AUTHORIZATION_PATH.search(path):
        tags.append("authorization")
    if _DEPLOYMENT_PATH.search(path):
        tags.append("deployment")
    if _SERVICE_BOUNDARY_PATH.search(path):
        tags.append("service_boundary")
    return tuple(tags or ("caller",))


def _path_tokens(path: str) -> set[str]:
    path_without_suffix = PurePosixPath(path).with_suffix("").as_posix()
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", path_without_suffix)
    return {
        part.lower()
        for part in re.findall(r"[A-Za-z0-9]+", separated)
        if len(part) >= 3 and part.lower() not in _IGNORED_PATH_TOKENS
    }


def _codegraph_relationship_suggestions(
    repository: Path,
    context: ReviewContext,
    changed_symbols_by_path: dict[str, set[str]],
    tracked_paths: set[str],
    *,
    enabled: bool,
    max_candidates: int,
) -> dict:
    """Read bounded caller and callee edges from a current index for this checkout."""
    summary = {
        "status": "not_requested",
        "repository_head_sha": None,
        "symbols_considered": 0,
        "symbols_checked": 0,
        "query_budget": min(_CODEGRAPH_MAX_SYMBOL_QUERIES, max_candidates * 2),
        "caller_queries_checked": 0,
        "callee_queries_checked": 0,
        "caller_query_failures": 0,
        "callee_query_failures": 0,
        "caller_candidates": [],
        "callee_candidates": [],
        "query_failures": 0,
        "truncated": False,
        "symbols_omitted_by_limit": 0,
        "omitted_symbol_examples": [],
        "exhaustive": False,
    }
    if not enabled:
        return summary
    executable = shutil.which("codegraph")
    if executable is None:
        summary["status"] = "not_installed"
        return summary
    head = _git(repository, "rev-parse", "HEAD").decode("ascii").strip().lower()
    summary["repository_head_sha"] = head
    if head != context.head_sha.lower():
        summary["status"] = "revision_mismatch"
        return summary

    try:
        result = subprocess.run(
            [executable, "status", "--json", str(repository)],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        summary["status"] = "status_unavailable"
        return summary
    if result.returncode:
        summary["status"] = "status_unavailable"
        return summary
    try:
        index_status = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        summary["status"] = "invalid_status"
        return summary
    if not isinstance(index_status, dict) or not index_status.get("initialized"):
        summary["status"] = "not_indexed"
        return summary
    project_path = index_status.get("projectPath")
    try:
        same_project = Path(project_path).resolve() == repository.resolve()
    except (TypeError, OSError):
        same_project = False
    pending = index_status.get("pendingChanges")
    pending_clean = isinstance(pending, dict) and all(
        type(pending.get(key)) is int and pending[key] == 0
        for key in ("added", "modified", "removed")
    )
    index_data = index_status.get("index")
    index_complete = isinstance(index_data, dict) and index_data.get("state") == "complete"
    if (
        not same_project
        or not pending_clean
        or index_status.get("worktreeMismatch", "missing") is not None
        or not index_complete
    ):
        summary["status"] = "index_not_current"
        return summary

    symbols = [
        (path, symbol)
        for path in context.changed_files
        for symbol in sorted(changed_symbols_by_path.get(path, ()))
    ]
    summary["symbols_considered"] = len(symbols)
    query_budget = summary["query_budget"]
    caller_candidates: dict[tuple[str, str, int], dict] = {}
    callee_candidates: dict[tuple[str, str, int], dict] = {}
    query_deadline = time.monotonic() + 10
    stop = False
    for path, symbol in symbols[:query_budget]:
        remaining_seconds = query_deadline - time.monotonic()
        if remaining_seconds <= 0:
            summary["truncated"] = True
            break
        summary["symbols_checked"] += 1
        for relation, candidates in (
            ("callers", caller_candidates),
            ("callees", callee_candidates),
        ):
            remaining_seconds = query_deadline - time.monotonic()
            if remaining_seconds <= 0:
                summary["truncated"] = True
                break
            summary[f"{relation[:-1]}_queries_checked"] += 1
            try:
                query = subprocess.run(
                    [
                        executable,
                        relation,
                        symbol,
                        "--path",
                        str(repository),
                        "--file",
                        path,
                        "--limit",
                        "20",
                        "--json",
                    ],
                    capture_output=True,
                    check=False,
                    text=True,
                    timeout=min(2, max(0.1, remaining_seconds)),
                )
            except (OSError, subprocess.TimeoutExpired):
                summary["query_failures"] += 1
                summary[f"{relation[:-1]}_query_failures"] += 1
                continue
            if query.returncode:
                summary["query_failures"] += 1
                summary[f"{relation[:-1]}_query_failures"] += 1
                continue
            try:
                payload = json.loads(query.stdout)
            except (TypeError, json.JSONDecodeError):
                summary["query_failures"] += 1
                summary[f"{relation[:-1]}_query_failures"] += 1
                continue
            if not isinstance(payload, dict):
                summary["query_failures"] += 1
                summary[f"{relation[:-1]}_query_failures"] += 1
                continue
            summary["truncated"] = summary["truncated"] or payload.get("truncated") is True
            definitions = payload.get("definitions")
            if not isinstance(definitions, list):
                summary["query_failures"] += 1
                summary[f"{relation[:-1]}_query_failures"] += 1
                continue
            for item in definitions:
                if not isinstance(item, dict):
                    continue
                summary["truncated"] = summary["truncated"] or item.get("truncated") is True
                definition = item.get("definition")
                related_nodes = item.get(relation)
                if not isinstance(definition, dict) or definition.get("filePath") != path:
                    continue
                if not isinstance(related_nodes, list):
                    continue
                for node in related_nodes:
                    if not isinstance(node, dict):
                        continue
                    related_path = node.get("filePath")
                    line = node.get("startLine")
                    if (
                        not isinstance(related_path, str)
                        or related_path not in tracked_paths
                        or related_path in context.changed_files
                        or type(line) is not int
                        or line < 1
                    ):
                        continue
                    key = (related_path, symbol, line)
                    candidates[key] = {
                        "path": related_path,
                        "symbol": symbol,
                        "changed_path": path,
                        "start_line": line,
                        "kind": node.get("kind") if isinstance(node.get("kind"), str) else None,
                    }
                    if len(caller_candidates) + len(callee_candidates) >= _CODEGRAPH_MAX_RELATION_CANDIDATES:
                        summary["truncated"] = True
                        stop = True
                        break
                if stop:
                    break
            if stop:
                break
        if stop:
            break

    summary["caller_candidates"] = [
        caller_candidates[key] for key in sorted(caller_candidates)
    ]
    summary["callee_candidates"] = [
        callee_candidates[key] for key in sorted(callee_candidates)
    ]
    omitted_symbols = symbols[query_budget:]
    summary["symbols_omitted_by_limit"] = len(omitted_symbols)
    summary["omitted_symbol_examples"] = [
        {"path": path, "symbol": symbol}
        for path, symbol in omitted_symbols[:20]
    ]
    summary["status"] = "partial" if summary["query_failures"] else "ready"
    if len(symbols) > query_budget:
        summary["truncated"] = True
    return summary


def discover_neighbor_evidence(
    context: ReviewContext,
    repository: Path,
    *,
    max_candidates: int = 50,
    use_codegraph: bool = False,
) -> dict:
    """Suggest pinned declarations, callers, contracts, tests and test manifests.

    This is a bounded Git text scan. It deliberately returns suggestions only: it
    does not add them to the bound context or claim that the result is exhaustive.
    """
    if not 1 <= max_candidates <= _DISCOVERY_MAX_CANDIDATES:
        raise ValueError("neighbor candidate limit must be between 1 and 200")
    pinned_diff = _diff(context, repository)
    diff_hunk_inventory = _diff_hunk_inventory(context, pinned_diff)
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
    from qa_orchestrator.repo_audit import audit_test_source

    test_audit_by_path: dict[str, dict] = {}
    test_audit_files_scanned = 0
    defined_symbols: set[str] = set()
    defined_symbols_by_path: dict[str, set[str]] = {}
    referenced_symbols: set[str] = set()
    referenced_module_paths: set[str] = set()
    total_bytes = 0
    scanned_sources = 0
    skipped_by_limits = 0
    collect_changed_symbols = True
    changed_sources_skipped_symbol_scan = 0
    changed_sources_skipped_symbol_scan_paths = []
    changed_symbol_budget_reached_path = None
    changed_source_paths_skipped_by_size = []
    for path in sorted(changed & tracked_set):
        size = int(_git(repository, "cat-file", "-s", f"{context.head_sha}:{path}"))
        if size > _DISCOVERY_MAX_SOURCE_BYTES or total_bytes + size > _DISCOVERY_MAX_TOTAL_BYTES:
            skipped_by_limits += 1
            changed_source_paths_skipped_by_size.append(path)
            continue
        source = _git(repository, "show", f"{context.head_sha}:{path}")
        total_bytes += len(source)
        scanned_sources += 1
        if collect_changed_symbols:
            path_symbols = _defined_symbols(path, source)
            defined_symbols_by_path[path] = path_symbols
            defined_symbols.update(path_symbols)
            referenced_symbols.update(_referenced_symbols(path, source, tracked_set))
            if len(defined_symbols | referenced_symbols) >= _DISCOVERY_MAX_SYMBOLS:
                collect_changed_symbols = False
                changed_symbol_budget_reached_path = path
        else:
            changed_sources_skipped_symbol_scan += 1
            changed_sources_skipped_symbol_scan_paths.append(path)
        referenced_module_paths.update(_referenced_module_paths(path, source, tracked_set))
        if _TEST_PATH.search(path):
            test_audit_by_path[path] = audit_test_source(path, source)
            test_audit_files_scanned += 1
    ordered_defined_symbols = tuple(
        sorted(defined_symbols, key=lambda name: (-len(name), name))[:_DISCOVERY_MAX_SYMBOLS]
    )
    ordered_referenced_symbols = tuple(
        sorted(referenced_symbols, key=lambda name: (-len(name), name))[:_DISCOVERY_MAX_SYMBOLS]
    )
    ordered_symbols = tuple(
        dict.fromkeys((*ordered_referenced_symbols, *ordered_defined_symbols))
    )[:_DISCOVERY_MAX_SYMBOLS]

    module_names = []
    for path in sorted(changed):
        name = PurePosixPath(path).stem
        if name in {"__init__", "index"}:
            name = PurePosixPath(path).parent.name
        if _IDENTIFIER.fullmatch(name) and name.lower() not in _IGNORED_IDENTIFIERS:
            module_names.append(name)
    all_module_search_terms = tuple(dict.fromkeys((*module_names, *ordered_symbols)))
    module_search_terms = all_module_search_terms[:_DISCOVERY_MAX_MODULE_SEARCH_TERMS]
    codegraph = _codegraph_relationship_suggestions(
        repository,
        context,
        defined_symbols_by_path,
        tracked_set,
        enabled=use_codegraph,
        max_candidates=max_candidates,
    )
    codegraph_by_path: dict[str, list[dict]] = {}
    for item in codegraph["caller_candidates"]:
        codegraph_by_path.setdefault(item["path"], []).append(item)
    codegraph_callees_by_path: dict[str, list[dict]] = {}
    for item in codegraph["callee_candidates"]:
        codegraph_callees_by_path.setdefault(item["path"], []).append(item)

    changed_path_tokens = set().union(*(_path_tokens(path) for path in changed)) if changed else set()
    symbol_path_tokens = (
        set().union(*(_path_tokens(symbol) for symbol in ordered_symbols))
        if ordered_symbols
        else set()
    )
    changed_directories = {PurePosixPath(path).parent for path in changed}
    dependency_manifest_paths = _test_dependency_manifests(changed, tracked_set)

    test_paths = set()
    ranked_symbol_paths = []
    for path in tracked:
        path_tokens = _path_tokens(path)
        if _TEST_PATH.search(path) and (path_tokens & changed_path_tokens):
            test_paths.add(path)
        if path in changed or (not ordered_symbols and path not in referenced_module_paths):
            continue
        changed_matches = path_tokens & changed_path_tokens
        symbol_matches = path_tokens & symbol_path_tokens
        same_directory = PurePosixPath(path).parent in changed_directories
        if not (path in referenced_module_paths or changed_matches or symbol_matches or same_directory):
            continue
        categories = _neighbor_categories(path)
        priority = (
            0 if path in referenced_module_paths else
            1 if path in codegraph_by_path or path in codegraph_callees_by_path else
            2 if "test" in categories and changed_matches else
            3 if set(categories) & _SYSTEM_BOUNDARY_CATEGORIES
            and (changed_matches or symbol_matches) else
            4 if symbol_matches else
            5 if same_directory else 6
        )
        ranked_symbol_paths.append(
            (priority, -len(symbol_matches), -len(changed_matches), path)
        )
    ranked_symbol_paths.sort()
    symbol_search_candidate_paths = tuple(item[3] for item in ranked_symbol_paths)
    ranked_search_paths = symbol_search_candidate_paths[:_DISCOVERY_MAX_SYMBOL_SEARCH_PATHS]
    symbol_search_paths = []
    symbol_search_bytes = 0
    symbol_search_paths_skipped_by_size = 0
    symbol_search_paths_skipped_by_size_examples = []
    for path in ranked_search_paths:
        size = int(_git(repository, "cat-file", "-s", f"{context.head_sha}:{path}"))
        if (
            size > _DISCOVERY_MAX_SOURCE_BYTES
            or symbol_search_bytes + size > _DISCOVERY_MAX_SYMBOL_SEARCH_BYTES
            or total_bytes + symbol_search_bytes + size > _DISCOVERY_MAX_TOTAL_BYTES
        ):
            skipped_by_limits += 1
            symbol_search_paths_skipped_by_size += 1
            symbol_search_paths_skipped_by_size_examples.append(path)
            continue
        symbol_search_paths.append(path)
        symbol_search_bytes += size
    symbol_search_paths = tuple(symbol_search_paths)

    grep_paths: set[str] = set()
    if ordered_symbols and symbol_search_paths:
        grep_output_bytes = 0
        for start in range(0, len(symbol_search_paths), _DISCOVERY_SYMBOL_SEARCH_BATCH_SIZE):
            path_batch = symbol_search_paths[
                start : start + _DISCOVERY_SYMBOL_SEARCH_BATCH_SIZE
            ]
            command = ["git", "-C", str(repository), "grep", "-I", "-F", "-w", "-l", "-z"]
            for symbol in ordered_symbols:
                command.extend(("-e", symbol))
            command.extend((context.head_sha, "--"))
            command.extend(f":(literal){path}" for path in path_batch)
            result = subprocess.run(command, capture_output=True, check=False)
            if result.returncode not in {0, 1}:
                raise ValueError("pinned Git symbol search failed")
            grep_output_bytes += len(result.stdout)
            if grep_output_bytes > _DISCOVERY_MAX_TOTAL_BYTES:
                raise ValueError("neighbor scan result exceeds the output limit")
            try:
                grep_paths.update(
                    part.decode("utf-8").split(":", 1)[-1]
                    for part in result.stdout.split(b"\0")
                    if part
                )
            except UnicodeDecodeError as exc:
                raise ValueError("neighbor paths must be valid UTF-8") from exc

    module_name_search_paths: set[str] = set()
    module_name_search_bytes = 0
    if module_search_terms:
        command = ["git", "-C", str(repository), "grep", "-I", "-F", "-w", "-l", "-z", "-i"]
        for term in module_search_terms:
            command.extend(("-e", term))
        command.extend((context.head_sha, "--"))
        result = subprocess.run(command, capture_output=True, check=False)
        if result.returncode not in {0, 1}:
            raise ValueError("pinned module-name search failed")
        module_name_search_bytes = len(result.stdout)
        if module_name_search_bytes > _DISCOVERY_MAX_MODULE_SEARCH_BYTES:
            raise ValueError("module-name search result exceeds the output limit")
        try:
            module_name_search_paths.update(
                part.decode("utf-8").split(":", 1)[-1]
                for part in result.stdout.split(b"\0")
                if part
            )
        except UnicodeDecodeError as exc:
            raise ValueError("module-name search paths must be valid UTF-8") from exc

    candidates = []
    all_candidate_paths = sorted(
        (
            grep_paths
            | module_name_search_paths
            | referenced_module_paths
            | test_paths
            | dependency_manifest_paths
            | set(codegraph_by_path)
            | set(codegraph_callees_by_path)
        )
        - changed,
        key=lambda path: (
            0 if path in dependency_manifest_paths else
            1 if path in referenced_module_paths else
            2 if path in codegraph_by_path or path in codegraph_callees_by_path else
            3 if path in module_name_search_paths else
            4 if path in test_paths else
            5 if path in grep_paths and "test" in _neighbor_categories(path) else
            6 if path in grep_paths and set(_neighbor_categories(path)) & _SYSTEM_BOUNDARY_CATEGORIES else
            7 if path in grep_paths else 8,
            path,
        ),
    )
    candidate_scan_limit = min(_DISCOVERY_MAX_CANDIDATE_SCAN_PATHS, max_candidates * 2)
    candidate_paths = all_candidate_paths[:candidate_scan_limit]
    candidate_paths_skipped_by_size = []
    for path in candidate_paths:
        if path not in tracked_set:
            continue
        size = int(_git(repository, "cat-file", "-s", f"{context.head_sha}:{path}"))
        if (
            size > _DISCOVERY_MAX_SOURCE_BYTES
            or total_bytes + symbol_search_bytes + size > _DISCOVERY_MAX_TOTAL_BYTES
        ):
            skipped_by_limits += 1
            candidate_paths_skipped_by_size.append(path)
            continue
        source = _git(repository, "show", f"{context.head_sha}:{path}")
        total_bytes += len(source)
        scanned_sources += 1
        try:
            text = source.decode("utf-8")
        except UnicodeDecodeError:
            continue
        test_audit = (
            audit_test_source(path, source) if _TEST_PATH.search(path) else None
        )
        if test_audit is not None:
            test_audit_by_path[path] = test_audit
            test_audit_files_scanned += 1
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
        categories = list(_neighbor_categories(path))
        codegraph_matches = codegraph_by_path.get(path, [])
        codegraph_callee_matches = codegraph_callees_by_path.get(path, [])
        if codegraph_matches and "caller" not in categories:
            categories.append("caller")
        if codegraph_callee_matches and not codegraph_matches and categories == ["caller"]:
            categories = ["callee"]
        elif codegraph_callee_matches and "callee" not in categories:
            categories.append("callee")
        imported_changed_paths = sorted(
            _referenced_module_paths(path, source, tracked_set) & changed
        )
        declarations = set(matches_by_symbol) & set(_defined_symbols(path, source))
        if declarations and categories == ["caller"]:
            categories = ["declaration"]
        elif declarations:
            categories.append("declaration")
        if path in dependency_manifest_paths:
            if categories == ["caller"]:
                categories = ["dependency"]
            else:
                categories.append("dependency")
        reasons = []
        if matches:
            reasons.append("symbol_reference")
        if codegraph_matches:
            reasons.append("codegraph_caller_edge")
        if codegraph_callee_matches:
            reasons.append("codegraph_callee_edge")
        if declarations:
            reasons.append("symbol_declaration")
        if "test" in categories and path in test_paths:
            reasons.append("test_path_convention")
        if "contract" in categories and matches:
            reasons.append("contract_path_and_symbol_reference")
        if set(categories) & (_SYSTEM_BOUNDARY_CATEGORIES - {"contract"}) and matches:
            reasons.append("system_boundary_path_and_symbol_reference")
        if path in dependency_manifest_paths:
            reasons.append("test_dependency_manifest")
        if path in referenced_module_paths:
            reasons.append("local_module_import")
        if imported_changed_paths:
            categories.append("importer")
            reasons.append("imports_changed_path")
        if test_audit and test_audit["candidate_count"]:
            reasons.extend(
                f"test_audit_{candidate['category']}"
                for candidate in test_audit["candidates"]
            )
        if not reasons:
            continue
        candidates.append(
            {
                "path": path,
                "categories": list(categories),
                "matched_symbols": matches,
                "codegraph_callers": codegraph_matches,
                "codegraph_callees": codegraph_callee_matches,
                "reasons": reasons,
                "imported_changed_paths": imported_changed_paths,
                "test_audit_candidates": test_audit["candidates"] if test_audit else [],
                "size_bytes": len(source),
                "sha256": hashlib.sha256(source).hexdigest(),
            }
        )

    candidates.sort(
        key=lambda item: (
            0 if "dependency" in item["categories"] else
            1 if set(item["categories"]) & _SYSTEM_BOUNDARY_CATEGORIES else
            2 if "declaration" in item["categories"] else
            3 if "test" in item["categories"] else 4,
            item["path"],
        )
    )
    candidates = candidates[:max_candidates]
    used_refs = set(context.evidence_refs)
    test_audit_evidence_refs_by_path = {
        entry.path: entry.evidence_ref
        for entry in context.evidence_registry
        if entry.kind == "source" and entry.revision == context.head_sha and entry.path
    }
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
            source_text_available=True,
        )
        used_refs.add(record.evidence_ref)
        next_id += 1
        test_audit_evidence_refs_by_path[candidate["path"]] = record.evidence_ref
        candidate["test_audit_candidates"] = [
            {**audit_candidate, "evidence_ref": record.evidence_ref}
            for audit_candidate in candidate["test_audit_candidates"]
        ]
        records.append({**candidate, "record": record.model_dump(mode="json")})

    candidate_manifest = [
        {
            "candidate_id": item["record"]["evidence_ref"],
            "relation": "discovery_candidate",
            "locator": item["path"],
        }
        for item in records
    ]
    candidate_set_sha256 = impact_candidate_set_sha256(
        tuple(
            (item["candidate_id"], item["relation"], item["locator"])
            for item in candidate_manifest
        )
    )

    omissions = []
    omission_reasons = {
        "changed_source_size_limit": "Changed source exceeded a per-file or total-source byte cap.",
        "changed_symbol_budget": "Changed-source symbol extraction reached its configured symbol budget.",
        "symbol_search_path_limit": "Only the highest-ranked path-related files are searched for symbol matches.",
        "symbol_search_source_size_limit": "A selected symbol-search source exceeded a byte cap.",
        "candidate_path_limit": "Only the highest-ranked candidate paths are opened and read.",
        "candidate_source_size_limit": "A candidate source exceeded a per-file or total-source byte cap.",
        "module_search_term_limit": "Only the configured number of module-name and symbol terms are searched.",
        "codegraph_symbol_query_limit": "Only the configured number of changed symbols receive CodeGraph queries.",
    }

    def add_omission(kind: str, count: int, examples: list, limit: int | None = None) -> None:
        if count:
            omission = {
                "kind": kind,
                "reason": omission_reasons[kind],
                "count": count,
                "examples": examples[:_DISCOVERY_MAX_OMISSION_EXAMPLES],
            }
            if limit is not None:
                omission["limit"] = limit
            omissions.append(omission)

    add_omission(
        "changed_source_size_limit",
        len(changed_source_paths_skipped_by_size),
        changed_source_paths_skipped_by_size,
        _DISCOVERY_MAX_SOURCE_BYTES,
    )
    add_omission(
        "changed_symbol_budget",
        changed_sources_skipped_symbol_scan + int(changed_symbol_budget_reached_path is not None),
        ([changed_symbol_budget_reached_path] if changed_symbol_budget_reached_path else [])
        + changed_sources_skipped_symbol_scan_paths,
        _DISCOVERY_MAX_SYMBOLS,
    )
    add_omission(
        "symbol_search_path_limit",
        len(symbol_search_candidate_paths) - len(ranked_search_paths),
        list(symbol_search_candidate_paths[len(ranked_search_paths) :]),
        _DISCOVERY_MAX_SYMBOL_SEARCH_PATHS,
    )
    add_omission(
        "symbol_search_source_size_limit",
        symbol_search_paths_skipped_by_size,
        symbol_search_paths_skipped_by_size_examples,
        _DISCOVERY_MAX_SYMBOL_SEARCH_BYTES,
    )
    add_omission(
        "candidate_path_limit",
        len(all_candidate_paths) - len(candidate_paths),
        list(all_candidate_paths[len(candidate_paths) :]),
        candidate_scan_limit,
    )
    add_omission(
        "candidate_source_size_limit",
        len(candidate_paths_skipped_by_size),
        candidate_paths_skipped_by_size,
        _DISCOVERY_MAX_SOURCE_BYTES,
    )
    add_omission(
        "module_search_term_limit",
        len(all_module_search_terms) - len(module_search_terms),
        list(all_module_search_terms[len(module_search_terms) :]),
        _DISCOVERY_MAX_MODULE_SEARCH_TERMS,
    )
    add_omission(
        "codegraph_symbol_query_limit",
        codegraph["symbols_omitted_by_limit"],
        codegraph["omitted_symbol_examples"],
        codegraph["query_budget"],
    )

    traced_relations = [
        {
            "kind": "changed_symbols_and_text_references",
            "method": "bounded pinned-source extraction and path-ranked literal search",
            "exhaustive": False,
        },
        {
            "kind": "local_imports_and_reverse_importers",
            "languages": ["Python", "JavaScript", "TypeScript"],
            "method": "bounded pinned-source import parsing",
            "exhaustive": False,
        },
        {
            "kind": "tests_and_test_dependencies",
            "method": "path conventions, selected test audits, and ancestor manifests",
            "exhaustive": False,
        },
        {
            "kind": "system_boundary_path_candidates",
            "categories": sorted(_SYSTEM_BOUNDARY_CATEGORIES),
            "method": "path classification plus bounded symbol/reference matches",
            "semantic_links_verified": False,
        },
        {
            "kind": "codegraph_callers_and_callees",
            "status": codegraph["status"],
            "symbols_checked": codegraph["symbols_checked"],
            "caller_queries_checked": codegraph["caller_queries_checked"],
            "callee_queries_checked": codegraph["callee_queries_checked"],
            "truncated": codegraph["truncated"],
            "exhaustive": False,
        },
    ]
    not_systematically_traced = [
        {"kind": "api_and_serialized_contract_registration", "reason": "Framework route registries and serialized contracts may not have direct imports or matching identifiers."},
        {"kind": "database_model_schema_migration_query_relations", "reason": "Path labels and text matches do not establish ORM, migration, or query semantics."},
        {"kind": "event_names_subscribers_and_topics", "reason": "String-based event wiring and subscribers are not resolved as semantic edges."},
        {"kind": "configuration_flags_and_authorization_policies", "reason": "Path classification is heuristic and does not resolve runtime flag or policy evaluation."},
        {"kind": "background_job_queue_and_scheduler_registration", "reason": "Queue and scheduler links are not resolved as semantic edges."},
        {"kind": "dynamic_generated_runtime_and_cross_repository_links", "reason": "These require host investigation and may be absent from the local index or checkout."},
    ]
    system_boundary_candidates = sorted(
        {
            category
            for candidate in records
            for category in candidate["categories"]
            if category in _SYSTEM_BOUNDARY_CATEGORIES
        }
    )

    payload = {
        "schema_version": 2,
        "method": "pinned_git_symbol_scan",
        "codegraph": codegraph,
        "base_sha": context.base_sha,
        "head_sha": context.head_sha,
        "diff_sha256": context.diff_sha256,
        "diff_hunk_inventory": diff_hunk_inventory,
        "changed_files": list(context.changed_files),
        "defined_symbols": list(ordered_defined_symbols),
        "referenced_symbols": list(ordered_referenced_symbols),
        "referenced_module_paths": sorted(referenced_module_paths),
        "searched_symbols": list(ordered_symbols),
        "module_search_terms": list(module_search_terms),
        "module_search_terms_omitted": max(
            0, len(all_module_search_terms) - len(module_search_terms)
        ),
        "module_name_search_paths": len(module_name_search_paths),
        "module_name_search_bytes": module_name_search_bytes,
        "module_name_search_bytes_limit": _DISCOVERY_MAX_MODULE_SEARCH_BYTES,
        "module_name_search_exhaustive": False,
        "changed_source_symbol_collection_truncated": not collect_changed_symbols,
        "changed_sources_skipped_symbol_scan": changed_sources_skipped_symbol_scan,
        "changed_sources_skipped_symbol_scan_examples": changed_sources_skipped_symbol_scan_paths[:_DISCOVERY_MAX_OMISSION_EXAMPLES],
        "candidate_records": records,
        "candidate_manifest": candidate_manifest,
        "candidate_count": len(candidate_manifest),
        "candidate_set_sha256": candidate_set_sha256,
        "candidate_limit": max_candidates,
        "candidate_paths_considered": len(candidate_paths),
        "candidate_paths_omitted_by_limit": max(0, len(all_candidate_paths) - len(candidate_paths)),
        "candidate_scan_limit": candidate_scan_limit,
        "symbol_search_scope": "ranked_path_candidates",
        "symbol_search_path_limit": _DISCOVERY_MAX_SYMBOL_SEARCH_PATHS,
        "symbol_search_bytes_limit": _DISCOVERY_MAX_SYMBOL_SEARCH_BYTES,
        "symbol_search_bytes_considered": symbol_search_bytes,
        "symbol_search_candidate_paths": len(symbol_search_candidate_paths),
        "symbol_search_paths_considered": len(symbol_search_paths),
        "symbol_search_paths_skipped_by_size": symbol_search_paths_skipped_by_size,
        "symbol_search_paths_omitted_by_limit": (
            len(symbol_search_candidate_paths) - len(ranked_search_paths)
        ),
        "symbol_search_paths_skipped_by_size_examples": symbol_search_paths_skipped_by_size_examples[:_DISCOVERY_MAX_OMISSION_EXAMPLES],
        "symbol_search_exhaustive": False,
        "relationship_coverage": {
            "exhaustive": False,
            "traced": traced_relations,
            "system_boundary_candidate_categories": system_boundary_candidates,
            "not_systematically_traced": not_systematically_traced,
        },
        "omissions": omissions,
        "test_audit": {
            "scope": "changed paths and selected neighbor test files",
            "test_files_scanned": test_audit_files_scanned,
            "javascript_test_declaration_scan_truncated_paths": [
                path
                for path, audit in sorted(test_audit_by_path.items())
                if audit["javascript_test_declaration_scan_truncated"]
            ],
            "source_decoding_error_paths": [
                path
                for path, audit in sorted(test_audit_by_path.items())
                if audit["source_decoding_error"]
            ],
            "candidate_count": sum(
                audit["candidate_count"] for audit in test_audit_by_path.values()
            ),
            "candidates": [
                {
                    **candidate,
                    "evidence_ref": test_audit_evidence_refs_by_path.get(path),
                }
                for path in sorted(test_audit_by_path)
                for candidate in test_audit_by_path[path]["candidates"]
            ],
            "limitations": [
                "Static candidates require source review and are not confirmed defects.",
                "Only changed paths and test files within the bounded neighbor scan are audited.",
                "Run qa-orch repo audit-tests for a bounded repository-wide scan.",
            ],
        },
        "source_files_scanned": scanned_sources,
        "sources_skipped_by_scan_limits": skipped_by_limits,
        "exhaustive": False,
        "notes": [
            "These are host-review suggestions, not an evidence-completeness claim.",
            "Symbol search is limited to ranked path-related files; missing callers are not evidence of absence.",
            "Module-name search and direct-import verification are bounded; omitted paths and terms remain possible.",
            "Changed source symbol extraction stops at its symbol budget, while direct import and changed-test scans continue within byte limits.",
            "Test-audit signals cover changed and selected neighbor test files only; use repo audit-tests for wider coverage.",
            "Use --codegraph to add caller and callee edges from a complete local index; stale, missing, or unbound indexes are reported and skipped.",
            "Review relationship_coverage.not_systematically_traced and omissions before treating the neighbor list as complete.",
            "Add accepted record objects and refs to the full registry before building the Evidence Packet.",
        ],
    }
    payload["discovery_sha256"] = neighbor_discovery_sha256(payload)
    return payload


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
        "discover",
        help="Suggest pinned relations, contracts, tests and test dependency manifests",
    )
    discover.add_argument("--max-candidates", default=50, type=int)
    discover.add_argument(
        "--codegraph",
        action="store_true",
        help="Add callers and callees from a complete CodeGraph index matching the bound checkout",
    )
    discover.add_argument(
        "--bound-context",
        type=Path,
        help="Write a review context bound to this discovery result and exact candidate set",
    )
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
            context,
            args.repository,
            max_candidates=args.max_candidates,
            use_codegraph=args.codegraph,
        )
        if args.bound_context:
            output_paths = {args.context.resolve()}
            if args.output:
                output_paths.add(args.output.resolve())
            if args.bound_context.resolve() in output_paths:
                raise ValueError("bound context must use a distinct output path")
            bound_context = bind_neighbor_discovery(context, payload)
            args.bound_context.write_text(bound_context.model_dump_json(), encoding="utf-8")
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
