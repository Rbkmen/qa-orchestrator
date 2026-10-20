"""Host-local evidence construction and verification against pinned Git objects."""

import argparse
import ast
import hashlib
import json
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

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
_DISCOVERY_MAX_CANDIDATE_SCAN_PATHS = 200
_DISCOVERY_MAX_SYMBOL_SEARCH_PATHS = 32
_DISCOVERY_MAX_SYMBOL_SEARCH_BYTES = 16_000_000
_DISCOVERY_MAX_MODULE_SEARCH_TERMS = 48
_DISCOVERY_MAX_MODULE_SEARCH_BYTES = 32_000_000
_DISCOVERY_SYMBOL_SEARCH_BATCH_SIZE = 32
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
        if entry.kind in {"diff", "requirement"}
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


def _neighbor_categories(path: str) -> tuple[str, ...]:
    tags = []
    if _TEST_PATH.search(path):
        tags.append("test")
    if _CONTRACT_PATH.search(path):
        tags.append("contract")
    return tuple(tags or ("caller",))


def _path_tokens(path: str) -> set[str]:
    path_without_suffix = PurePosixPath(path).with_suffix("").as_posix()
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", path_without_suffix)
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
    """Suggest pinned declarations, callers, contracts, tests and test manifests.

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
    from qa_orchestrator.repo_audit import audit_test_source

    test_audit_by_path: dict[str, dict] = {}
    test_audit_files_scanned = 0
    defined_symbols: set[str] = set()
    referenced_symbols: set[str] = set()
    referenced_module_paths: set[str] = set()
    total_bytes = 0
    scanned_sources = 0
    skipped_by_limits = 0
    collect_changed_symbols = True
    changed_sources_skipped_symbol_scan = 0
    for path in sorted(changed & tracked_set):
        size = int(_git(repository, "cat-file", "-s", f"{context.head_sha}:{path}"))
        if size > _DISCOVERY_MAX_SOURCE_BYTES or total_bytes + size > _DISCOVERY_MAX_TOTAL_BYTES:
            skipped_by_limits += 1
            continue
        source = _git(repository, "show", f"{context.head_sha}:{path}")
        total_bytes += len(source)
        scanned_sources += 1
        if collect_changed_symbols:
            defined_symbols.update(_defined_symbols(path, source))
            referenced_symbols.update(_referenced_symbols(path, source, tracked_set))
            if len(defined_symbols | referenced_symbols) >= _DISCOVERY_MAX_SYMBOLS:
                collect_changed_symbols = False
        else:
            changed_sources_skipped_symbol_scan += 1
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
            1 if "test" in categories and changed_matches else
            2 if "contract" in categories and (changed_matches or symbol_matches) else
            3 if symbol_matches else
            4 if same_directory else 5
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
    for path in ranked_search_paths:
        size = int(_git(repository, "cat-file", "-s", f"{context.head_sha}:{path}"))
        if (
            size > _DISCOVERY_MAX_SOURCE_BYTES
            or symbol_search_bytes + size > _DISCOVERY_MAX_SYMBOL_SEARCH_BYTES
            or total_bytes + symbol_search_bytes + size > _DISCOVERY_MAX_TOTAL_BYTES
        ):
            skipped_by_limits += 1
            symbol_search_paths_skipped_by_size += 1
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
        )
        - changed,
        key=lambda path: (
            0 if path in dependency_manifest_paths else
            1 if path in referenced_module_paths else
            2 if path in module_name_search_paths else
            3 if path in test_paths else
            4 if path in grep_paths and "test" in _neighbor_categories(path) else
            5 if path in grep_paths and "contract" in _neighbor_categories(path) else
            6 if path in grep_paths else 7,
            path,
        ),
    )
    candidate_scan_limit = min(_DISCOVERY_MAX_CANDIDATE_SCAN_PATHS, max_candidates * 2)
    candidate_paths = all_candidate_paths[:candidate_scan_limit]
    for path in candidate_paths:
        if path not in tracked_set:
            continue
        size = int(_git(repository, "cat-file", "-s", f"{context.head_sha}:{path}"))
        if (
            size > _DISCOVERY_MAX_SOURCE_BYTES
            or total_bytes + symbol_search_bytes + size > _DISCOVERY_MAX_TOTAL_BYTES
        ):
            skipped_by_limits += 1
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
        if declarations:
            reasons.append("symbol_declaration")
        if "test" in categories and path in test_paths:
            reasons.append("test_path_convention")
        if "contract" in categories and matches:
            reasons.append("contract_path_and_symbol_reference")
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
            1 if "declaration" in item["categories"] else
            2 if "test" in item["categories"] else
            3 if "contract" in item["categories"] else 4,
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

    return {
        "schema_version": 1,
        "method": "pinned_git_symbol_scan",
        "base_sha": context.base_sha,
        "head_sha": context.head_sha,
        "diff_sha256": context.diff_sha256,
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
        "candidate_records": records,
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
            len(symbol_search_candidate_paths) - len(symbol_search_paths)
        ),
        "symbol_search_exhaustive": False,
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
            "Reconcile callers/contracts with CodeGraph before binding neighbor evidence.",
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
        "discover",
        help="Suggest pinned declarations, callers, contracts, tests and test dependency manifests",
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
