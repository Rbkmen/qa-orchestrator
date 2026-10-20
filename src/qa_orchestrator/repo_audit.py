"""Bounded, read-only repository profiling and test-suite heuristics."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections import Counter
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from qa_orchestrator.config import Settings

_SCHEMA_VERSION = 1
_MAX_TRACKED_PATHS = 200_000
_MAX_TREE_BYTES = 64_000_000
_MAX_TEST_FILES = 5_000
_MAX_AUDIT_FILES = 1_000
_MAX_FILE_BYTES = 1_000_000
_MAX_PROFILE_CONFIG_BYTES = 2_000_000
_MAX_AUDIT_SOURCE_BYTES = 32_000_000
_MAX_REPORTED_TEST_PATHS = 500
_MAX_CANDIDATES = 500

_TEST_PATH = re.compile(r"(^|/)(tests?|specs?)(/|$)|(^|[._-])(test|spec)([._-]|$)", re.IGNORECASE)
_CAMEL_CASE_TEST_FILE = re.compile(
    r"(^|/)(Test|Tests|Spec|Specs)[^/]*\.[^/]+$|(?:Test|Tests|Spec|Specs)\.[^/]+$"
)
_CI_PATH = re.compile(
    r"(^|/)(\.github/workflows/[^/]+|\.gitlab-ci\.yml|Jenkinsfile|"
    r"azure-pipelines\.ya?ml|\.circleci/config\.ya?ml)$",
    re.IGNORECASE,
)
_FRAMEWORK_MARKERS = (
    ("pytest", re.compile(r"\bpytest\b", re.IGNORECASE)),
    ("unittest", re.compile(r"\bunittest\b", re.IGNORECASE)),
    ("jest", re.compile(r"\bjest\b", re.IGNORECASE)),
    ("vitest", re.compile(r"\bvitest\b", re.IGNORECASE)),
    ("playwright", re.compile(r"\bplaywright\b", re.IGNORECASE)),
    ("cypress", re.compile(r"\bcypress\b", re.IGNORECASE)),
    ("go test", re.compile(r"\bgo\s+test\b", re.IGNORECASE)),
    ("cargo test", re.compile(r"\bcargo\s+test\b", re.IGNORECASE)),
    ("rspec", re.compile(r"\brspec\b", re.IGNORECASE)),
    ("minitest", re.compile(r"\bminitest\b", re.IGNORECASE)),
    ("junit", re.compile(r"\bjunit\b", re.IGNORECASE)),
    ("testng", re.compile(r"\btestng\b", re.IGNORECASE)),
)
_CI_COMMANDS = (
    ("vitest", re.compile(r"\bvitest\b", re.IGNORECASE)),
    ("jest", re.compile(r"\bjest\b", re.IGNORECASE)),
    ("cypress run", re.compile(r"\bcypress\s+run\b", re.IGNORECASE)),
    ("uv run pytest", re.compile(r"\buv\s+run\s+pytest\b", re.IGNORECASE)),
    ("python -m pytest", re.compile(r"\bpython(?:3)?\s+-m\s+pytest\b", re.IGNORECASE)),
    ("python -m unittest", re.compile(r"\bpython(?:3)?\s+-m\s+unittest\b", re.IGNORECASE)),
    ("pytest", re.compile(r"\bpytest\b", re.IGNORECASE)),
    ("npm test", re.compile(r"\bnpm\s+(?:run\s+)?test(?:[-:\w]*)\b", re.IGNORECASE)),
    ("pnpm test", re.compile(r"\bpnpm\s+(?:run\s+)?test(?:[-:\w]*)\b", re.IGNORECASE)),
    ("yarn test", re.compile(r"\byarn\s+(?:run\s+)?test(?:[-:\w]*)\b", re.IGNORECASE)),
    ("npx playwright test", re.compile(r"\bnpx\s+playwright\s+test\b", re.IGNORECASE)),
    ("go test", re.compile(r"\bgo\s+test\b", re.IGNORECASE)),
    ("cargo test", re.compile(r"\bcargo\s+test\b", re.IGNORECASE)),
    ("bundle exec rspec", re.compile(r"\bbundle\s+exec\s+rspec\b", re.IGNORECASE)),
    ("mvn test", re.compile(r"\bmvn(?:w)?\s+(?:[^\n]*\s)?test\b", re.IGNORECASE)),
    ("gradle test", re.compile(r"\b(?:gradle|gradlew)\s+(?:[^\n]*\s)?test\b", re.IGNORECASE)),
)
_LANGUAGES = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rb": "ruby",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".swift": "swift",
    ".cs": "csharp",
}
_CONFIG_NAMES = {
    "pyproject.toml",
    "pytest.ini",
    "tox.ini",
    "setup.cfg",
    "package.json",
    "pnpm-workspace.yaml",
    "go.mod",
    "Cargo.toml",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "Gemfile",
    "Makefile",
    "justfile",
    ".gitlab-ci.yml",
    "Jenkinsfile",
    "azure-pipelines.yml",
    "azure-pipelines.yaml",
    ".circleci/config.yml",
    ".circleci/config.yaml",
}
_FLAKY_CALLS = {
    "sleep": "fixed_wait",
    "waitForTimeout": "fixed_wait",
    "setTimeout": "fixed_wait",
    "wait_for_timeout": "fixed_wait",
    "now": "time_or_random_dependency",
    "today": "time_or_random_dependency",
    "random": "time_or_random_dependency",
    "randint": "time_or_random_dependency",
    "uniform": "time_or_random_dependency",
}


def _git(repository: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repository), *args],
        input=input_bytes,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise ValueError("repository snapshot could not be read from Git")
    return result.stdout


def _snapshot(repository: Path, revision: str) -> tuple[Path, str, dict[str, str]]:
    try:
        root = Path(_git(repository, "rev-parse", "--show-toplevel").decode().strip()).resolve()
        commit = _git(repository, "rev-parse", f"{revision}^{{commit}}").decode().strip()
    except (UnicodeDecodeError, OSError) as exc:
        raise ValueError("repository path or revision is invalid") from exc
    raw_tree = _git(root, "ls-tree", "-r", "--full-tree", "-z", commit)
    if len(raw_tree) > _MAX_TREE_BYTES:
        raise ValueError("repository tree exceeds the scan size limit")
    objects: dict[str, str] = {}
    for record in raw_tree.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            _mode, kind, object_id = metadata.decode("ascii").split()
            path = raw_path.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("Git tree contains an unsupported path") from exc
        if kind == "blob":
            objects[path] = object_id
            if len(objects) > _MAX_TRACKED_PATHS:
                raise ValueError("repository exceeds the tracked path limit")
    return root, commit, objects


def _blob_sizes(repository: Path, object_ids: list[str]) -> dict[str, int]:
    if not object_ids:
        return {}
    unique_ids = list(dict.fromkeys(object_ids))
    output = _git(
        repository,
        "cat-file",
        "--batch-check",
        input_bytes=("\n".join(unique_ids) + "\n").encode(),
    )
    sizes: dict[str, int] = {}
    for line in output.splitlines():
        fields = line.decode("ascii").split()
        if len(fields) != 3 or fields[1] != "blob":
            raise ValueError("Git object is not a blob")
        sizes[fields[0]] = int(fields[2])
    if set(sizes) != set(unique_ids):
        raise ValueError("Git blob metadata is incomplete")
    return sizes


def _read_blobs(
    repository: Path,
    objects: dict[str, str],
    paths: list[str],
    *,
    max_file_bytes: int,
    max_total_bytes: int,
) -> tuple[dict[str, bytes], list[str]]:
    requested = [(path, objects[path]) for path in paths if path in objects]
    sizes = _blob_sizes(repository, [object_id for _, object_id in requested])
    selected: list[tuple[str, str]] = []
    skipped: list[str] = []
    total = 0
    for path, object_id in requested:
        size = sizes[object_id]
        if size > max_file_bytes or total + size > max_total_bytes:
            skipped.append(path)
            continue
        selected.append((path, object_id))
        total += size
    object_ids = list(dict.fromkeys(object_id for _, object_id in selected))
    if not object_ids:
        return {}, skipped
    output = _git(
        repository,
        "cat-file",
        "--batch",
        input_bytes=("\n".join(object_ids) + "\n").encode(),
    )
    blobs_by_id: dict[str, bytes] = {}
    offset = 0
    while offset < len(output):
        header_end = output.find(b"\n", offset)
        if header_end < 0:
            raise ValueError("Git blob response is malformed")
        fields = output[offset:header_end].decode("ascii").split()
        if len(fields) != 3 or fields[1] != "blob":
            raise ValueError("Git blob response is incomplete")
        object_id, _kind, size_text = fields
        size = int(size_text)
        content_start = header_end + 1
        content_end = content_start + size
        if content_end >= len(output) or output[content_end : content_end + 1] != b"\n":
            raise ValueError("Git blob response has an invalid size")
        blobs_by_id[object_id] = output[content_start:content_end]
        offset = content_end + 1
    return {path: blobs_by_id[object_id] for path, object_id in selected}, skipped


def _test_paths(paths: list[str]) -> list[str]:
    return [path for path in paths if _TEST_PATH.search(path) or _CAMEL_CASE_TEST_FILE.search(path)]


def _profile_inputs(paths: list[str]) -> list[str]:
    return sorted(
        path for path in paths if PurePosixPath(path).name in _CONFIG_NAMES or _CI_PATH.search(path)
    )


def _ci_command(line: str) -> str | None:
    line = re.sub(r"^\s*(?:-\s*)?(?:run|script|command)\s*:\s*", "", line, flags=re.IGNORECASE)
    line = re.sub(r"^\s*-\s*", "", line)
    for segment in re.split(r"&&|\|\||[;\n]", line):
        segment = segment.strip().strip("\"'")
        segment = re.sub(
            r"^(?:[A-Za-z_][A-Za-z0-9_]*=(?:\"[^\"]*\"|'[^']*'|[^\s]+)\s+)+",
            "",
            segment,
        )
        for command, pattern in _CI_COMMANDS:
            if pattern.match(segment):
                return command
    return None


def _profile_fingerprint(
    objects: dict[str, str], config_paths: list[str], test_paths: list[str]
) -> str:
    inputs = {
        "schema_version": _SCHEMA_VERSION,
        "configuration": [{"path": path, "git_blob": objects[path]} for path in config_paths],
        "test_paths": test_paths,
    }
    return hashlib.sha256(
        json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _language_inventory(paths: list[str]) -> list[dict[str, Any]]:
    extension_counts = Counter(
        _LANGUAGES[suffix]
        for path in paths
        if (suffix := PurePosixPath(path).suffix.lower()) in _LANGUAGES
    )
    return [
        {"name": name, "tracked_files": count}
        for name, count in sorted(extension_counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _profile_payload(
    repository: Path,
    revision: str,
    objects: dict[str, str],
    config_blobs: dict[str, bytes],
    fingerprint: str,
    omitted_config: int = 0,
) -> dict[str, Any]:
    paths = sorted(objects)
    test_paths = _test_paths(paths)
    config = [
        {
            "path": path,
            "git_blob": objects[path],
            "sha256": hashlib.sha256(config_blobs[path]).hexdigest(),
        }
        for path in sorted(config_blobs)
    ]
    frameworks: set[str] = set()
    ci_commands: list[dict[str, Any]] = []
    project_commands: list[dict[str, str]] = []
    for path, content in sorted(config_blobs.items()):
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for framework, marker in _FRAMEWORK_MARKERS:
            if marker.search(text):
                frameworks.add(framework)
        if _CI_PATH.search(path):
            for line_number, line in enumerate(text.splitlines(), 1):
                command = _ci_command(line)
                if command:
                    ci_commands.append({"path": path, "line": line_number, "command": command})
        if PurePosixPath(path).name == "package.json":
            try:
                package = json.loads(text)
            except json.JSONDecodeError:
                package = {}
            scripts = package.get("scripts", {}) if isinstance(package, dict) else {}
            if isinstance(scripts, dict):
                for script_name, script_body in sorted(scripts.items()):
                    if not (script_name == "test" or script_name.startswith("test:")):
                        continue
                    if not isinstance(script_body, str):
                        continue
                    command = _ci_command(script_body)
                    if command:
                        project_commands.append(
                            {"path": path, "script": script_name, "command": command}
                        )
    frameworks.update(
        framework
        for framework, marker in _FRAMEWORK_MARKERS
        if any(marker.search(command["command"]) for command in ci_commands)
    )
    ordered_tests = sorted(test_paths)
    return {
        "schema_version": _SCHEMA_VERSION,
        "repository": str(repository),
        "revision": revision,
        "profile_fingerprint": fingerprint,
        "languages": _language_inventory(paths),
        "test_frameworks": sorted(frameworks),
        "project_test_commands": project_commands[:500],
        "configuration": config,
        "configuration_omitted_by_size": omitted_config,
        "ci_test_commands": ci_commands[:500],
        "test_inventory": {
            "total": len(ordered_tests),
            "paths": ordered_tests[:_MAX_REPORTED_TEST_PATHS],
            "omitted_paths": max(0, len(ordered_tests) - _MAX_REPORTED_TEST_PATHS),
        },
        "critical_flows": {"configured": False, "items": []},
    }


def _cache_path(repository: Path, cache_dir: Path) -> Path:
    repository_key = hashlib.sha256(str(repository).encode()).hexdigest()
    return cache_dir / "repo-profiles" / f"{repository_key}.json"


def _write_cache(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _profile_for_snapshot(
    root: Path, commit: str, objects: dict[str, str], *, cache_dir: Path | None, use_cache: bool
) -> dict[str, Any]:
    config_paths = _profile_inputs(sorted(objects))
    fingerprint = _profile_fingerprint(objects, config_paths, _test_paths(sorted(objects)))
    selected_cache_dir = cache_dir or Settings.from_env().data_dir
    path = _cache_path(root, Path(selected_cache_dir))
    if use_cache:
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            cached = None
        except json.JSONDecodeError:
            cached = None
        if (
            isinstance(cached, dict)
            and cached.get("schema_version") == _SCHEMA_VERSION
            and cached.get("profile_fingerprint") == fingerprint
        ):
            profile = {**cached, "revision": commit, "repository": str(root)}
            profile["languages"] = _language_inventory(sorted(objects))
            profile["cache"] = {"enabled": True, "hit": True}
            return profile
    config_blobs, skipped = _read_blobs(
        root,
        objects,
        config_paths,
        max_file_bytes=_MAX_PROFILE_CONFIG_BYTES,
        max_total_bytes=_MAX_PROFILE_CONFIG_BYTES,
    )
    profile = _profile_payload(root, commit, objects, config_blobs, fingerprint, len(skipped))
    if use_cache:
        _write_cache(path, profile)
        profile["cache"] = {"enabled": True, "hit": False}
        return profile
    profile["cache"] = {"enabled": False, "hit": False}
    return profile


def build_repository_profile(
    repository: Path,
    revision: str = "HEAD",
    *,
    cache_dir: Path | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Summarize tracked project and test metadata; cache by config and test-path fingerprint."""
    root, commit, objects = _snapshot(Path(repository), revision)
    return _profile_for_snapshot(root, commit, objects, cache_dir=cache_dir, use_cache=use_cache)


def _call_name(node: ast.expr) -> str | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _function_scope_nodes(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[ast.AST]:
    pending = list(function.body)
    nodes = []
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        nodes.append(node)
        pending.extend(ast.iter_child_nodes(node))
    return nodes


def _python_tests(source: str) -> tuple[int, list[dict], int, list[tuple[int, str]]]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 0, [], 0, []
    test_count = 0
    candidates: list[dict] = []
    candidate_count = 0
    duplicate_bodies: list[tuple[int, str]] = []
    class_stack: list[str] = []
    function_depth = 0

    def record_candidate(category: str, line: int) -> None:
        nonlocal candidate_count
        candidate_count += 1
        if len(candidates) < _MAX_CANDIDATES:
            candidates.append({"category": category, "line": line})

    class Visitor(ast.NodeVisitor):
        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            class_stack.append(node.name)
            self.generic_visit(node)
            class_stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_test(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_test(node)

        def _visit_test(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            nonlocal function_depth, test_count
            test_case_class = any(
                name.startswith("Test") or name.endswith("TestCase") for name in class_stack
            )
            if (
                node.name.startswith("test_")
                and function_depth == 0
                and (not class_stack or test_case_class)
            ):
                test_count += 1
                body_dump = ast.dump(
                    ast.Module(body=node.body, type_ignores=[]), include_attributes=False
                )
                if body_dump:
                    duplicate_bodies.append(
                        (node.lineno, hashlib.sha256(body_dump.encode()).hexdigest())
                    )
                body_nodes = _function_scope_nodes(node)
                calls = [
                    (_call_name(child.func) or "", child.lineno)
                    for child in body_nodes
                    if isinstance(child, ast.Call)
                ]
                has_assertion = any(
                    isinstance(child, ast.Assert)
                    or (
                        isinstance(child, ast.Call)
                        and (
                            (_call_name(child.func) or "").split(".")[-1].startswith("assert")
                            or (_call_name(child.func) or "").endswith("pytest.raises")
                        )
                    )
                    for child in body_nodes
                )
                if not has_assertion:
                    record_candidate("no_direct_assertion", node.lineno)
                for call_name, line in calls:
                    name = call_name.split(".")[-1]
                    category = _FLAKY_CALLS.get(name)
                    if category:
                        record_candidate(category, line)
                    if name in {"skip", "skipif", "xfail"}:
                        record_candidate("skipped_test", line)
                for decorator in node.decorator_list:
                    decorator_name = _call_name(
                        decorator.func if isinstance(decorator, ast.Call) else decorator
                    )
                    if decorator_name and any(
                        token in decorator_name.lower() for token in ("skip", "xfail")
                    ):
                        record_candidate("skipped_test", decorator.lineno)
            function_depth += 1
            self.generic_visit(node)
            function_depth -= 1

    Visitor().visit(tree)
    return test_count, candidates, candidate_count, duplicate_bodies


_JS_ASSERTION = re.compile(r"\b(?:expect|assert)\s*\(|\.to(?:Be|Equal|Match|Contain|Have|Throw)\b")
_JS_FLAKY = re.compile(r"\b(?:waitForTimeout|setTimeout)\s*\(")


def _line_at(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


def _mask_javascript_non_code(source: str) -> str:
    """Blank comments and quoted strings while preserving offsets and newlines."""
    masked = list(source)
    index = 0
    while index < len(source):
        if source.startswith("//", index):
            end = source.find("\n", index + 2)
            end = len(source) if end < 0 else end
            for position in range(index, end):
                masked[position] = " "
            index = end
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            end = len(source) if end < 0 else end + 2
            for position in range(index, end):
                if source[position] != "\n":
                    masked[position] = " "
            index = end
            continue
        if source[index] in {"'", '"', "`"}:
            quote = source[index]
            end = index + 1
            while end < len(source):
                if source[end] == "\\":
                    end += 2
                    continue
                if source[end] == quote:
                    end += 1
                    break
                end += 1
            for position in range(index, min(end, len(source))):
                if source[position] != "\n":
                    masked[position] = " "
            index = end
            continue
        index += 1
    return "".join(masked)


def _matching_javascript_delimiter(
    source: str, opening: int, *, max_scan: int
) -> tuple[int | None, int]:
    opener = source[opening]
    closer = {"(": ")", "[": "]", "{": "}"}[opener]
    depth = 0
    scanned = 0
    for position in range(opening, min(len(source), opening + max_scan)):
        scanned += 1
        if source[position] == opener:
            depth += 1
        elif source[position] == closer:
            depth -= 1
            if depth == 0:
                return position, scanned
    return None, scanned


def _skip_javascript_space(source: str, position: int) -> int:
    while position < len(source) and source[position].isspace():
        position += 1
    return position


def _javascript_test_declarations(source: str) -> list[dict[str, int | str | None]]:
    return _javascript_test_scan(source)[0]


def _javascript_test_scan(
    source: str,
) -> tuple[list[dict[str, int | str | None]], bool]:
    """Find test/it calls, including test.each(...)(...) and it.each(...)(...)."""
    declarations = []
    delimiter_scan_budget = len(source) * 4
    delimiter_chars_scanned = 0
    covered_until = -1
    for match in re.finditer(r"\b(?P<name>test|it)\b", source):
        if match.start() <= covered_until:
            continue
        cursor = match.end()
        mode = None
        while True:
            cursor = _skip_javascript_space(source, cursor)
            if cursor >= len(source) or source[cursor] != ".":
                break
            property_match = re.match(r"\.\s*([A-Za-z_$][\w$]*)", source[cursor:])
            if property_match is None:
                break
            property_name = property_match.group(1)
            cursor += property_match.end()
            if property_name == "each":
                cursor = _skip_javascript_space(source, cursor)
                if cursor >= len(source) or source[cursor] != "(":
                    break
                remaining_scan = delimiter_scan_budget - delimiter_chars_scanned
                if remaining_scan <= 0:
                    return declarations, True
                table_end, scanned = _matching_javascript_delimiter(
                    source, cursor, max_scan=remaining_scan
                )
                delimiter_chars_scanned += scanned
                if table_end is None:
                    return declarations, True
                cursor = table_end + 1
                continue
            if property_name in {"skip", "only", "todo"}:
                mode = property_name
                continue
            if property_name == "concurrent":
                continue
            break
        cursor = _skip_javascript_space(source, cursor)
        if cursor >= len(source) or source[cursor] != "(":
            continue
        remaining_scan = delimiter_scan_budget - delimiter_chars_scanned
        if remaining_scan <= 0:
            return declarations, True
        call_end, scanned = _matching_javascript_delimiter(
            source, cursor, max_scan=remaining_scan
        )
        delimiter_chars_scanned += scanned
        if call_end is None:
            return declarations, True
        declarations.append(
            {"start": match.start(), "open": cursor, "close": call_end, "mode": mode}
        )
        covered_until = call_end
    return declarations, False


def _test_candidates(
    path: str,
    source: bytes,
    *,
    javascript_masked: str | None = None,
    javascript_declarations: list[dict[str, int | str | None]] | None = None,
) -> tuple[list[dict], int]:
    suffix = PurePosixPath(path).suffix.lower()
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError:
        return [], 0
    if suffix in {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}:
        masked = javascript_masked or _mask_javascript_non_code(text)
        declarations = javascript_declarations
        if declarations is None:
            declarations, _ = _javascript_test_scan(masked)
        candidates = []
        candidate_count = 0
        seen_candidates = set()

        def add_candidate(category: str, offset: int) -> None:
            nonlocal candidate_count
            line = _line_at(text, offset)
            key = (category, line)
            if key in seen_candidates:
                return
            seen_candidates.add(key)
            candidate_count += 1
            if len(candidates) < _MAX_CANDIDATES:
                candidates.append({"category": category, "line": line})

        for declaration in declarations:
            body = masked[declaration["open"] + 1 : declaration["close"]]
            if not _JS_ASSERTION.search(body):
                add_candidate("no_direct_assertion", declaration["start"])
            mode = declaration["mode"]
            if mode in {"skip", "todo", "only"}:
                category = "skipped_test" if mode in {"skip", "todo"} else "focused_test"
                add_candidate(category, declaration["start"])
        for match in _JS_FLAKY.finditer(masked):
            add_candidate("fixed_wait", match.start())
        return candidates, candidate_count
    return [], 0


def _candidate_id(category: str, path: str, line: int, related: str = "") -> str:
    value = f"{category}\0{path}\0{line}\0{related}".encode()
    return "T-" + hashlib.sha256(value).hexdigest()[:12]


def _normalized_test_target(path: str) -> str:
    name = PurePosixPath(path).stem
    lowered = name.lower()
    for prefix in ("test_", "test-"):
        if lowered.startswith(prefix):
            name = name[len(prefix) :]
            break
    else:
        if (
            name.startswith(("Test", "test"))
            and len(name) > 4
            and name[4].isupper()
        ):
            name = name[4:]
    lowered = name.lower()
    for suffix in (".test", "_test", "-test", ".spec", "_spec", "-spec"):
        if lowered.endswith(suffix):
            name = name[: -len(suffix)]
            break
    else:
        if name.endswith(("Test", "Spec")) and len(name) > 4:
            name = name[:-4]
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def audit_test_source(path: str, source: bytes) -> dict[str, Any]:
    """Return bounded static test candidates for one pinned source blob."""
    suffix = PurePosixPath(path).suffix.lower()
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError:
        return {
            "tests_identified": 0,
            "candidate_count": 0,
            "candidates": [],
            "javascript_test_declaration_scan_truncated": False,
            "source_decoding_error": True,
        }
    if suffix == ".py":
        tests_identified, raw_candidates, candidate_count, _ = _python_tests(text)
        javascript_scan_truncated = False
    elif suffix in {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}:
        masked = _mask_javascript_non_code(text)
        declarations, javascript_scan_truncated = _javascript_test_scan(masked)
        tests_identified = len(declarations)
        raw_candidates, candidate_count = _test_candidates(
            path,
            source,
            javascript_masked=masked,
            javascript_declarations=declarations,
        )
    else:
        return {
            "tests_identified": 0,
            "candidate_count": 0,
            "candidates": [],
            "javascript_test_declaration_scan_truncated": False,
            "source_decoding_error": False,
        }
    source_sha256 = hashlib.sha256(source).hexdigest()
    candidates = [
        {
            "candidate_id": _candidate_id(candidate["category"], path, candidate["line"]),
            **candidate,
            "path": path,
            "evidence_sha256": source_sha256,
        }
        for candidate in raw_candidates[:_MAX_CANDIDATES]
    ]
    return {
        "tests_identified": tests_identified,
        "candidate_count": candidate_count,
        "candidates": candidates,
        "javascript_test_declaration_scan_truncated": javascript_scan_truncated,
        "source_decoding_error": False,
    }


def audit_test_suite(
    repository: Path,
    revision: str = "HEAD",
    *,
    max_files: int = _MAX_AUDIT_FILES,
    changed_paths: Sequence[str] = (),
    cache_dir: Path | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Report bounded, source-anchored test-suite candidates from committed Git blobs."""
    if not 1 <= max_files <= _MAX_TEST_FILES:
        raise ValueError(f"max_files must be between 1 and {_MAX_TEST_FILES}")
    if isinstance(changed_paths, str) or any(
        not isinstance(path, str) or not path.strip() for path in changed_paths
    ):
        raise ValueError("changed_paths must contain non-empty repository-relative paths")
    root, commit, objects = _snapshot(Path(repository), revision)
    paths = _test_paths(sorted(objects))
    changed = set(changed_paths)
    changed_targets = {
        _normalized_test_target(path)
        for path in changed
        if _normalized_test_target(path)
    }
    selected_paths = sorted(
        paths,
        key=lambda path: (
            0 if path in changed else
            1 if _normalized_test_target(path) in changed_targets else 2,
            path,
        ),
    )[:max_files]
    sources, skipped = _read_blobs(
        root,
        objects,
        selected_paths,
        max_file_bytes=_MAX_FILE_BYTES,
        max_total_bytes=_MAX_AUDIT_SOURCE_BYTES,
    )
    raw_candidates: list[dict[str, Any]] = []
    raw_candidate_count = 0
    duplicate_bodies: dict[str, tuple[str, int, str]] = {}
    javascript_scan_truncated_paths = []
    javascript_source_decoding_error_paths = []
    test_count = 0
    for path in sorted(sources):
        source = sources[path]
        if PurePosixPath(path).suffix.lower() == ".py":
            file_test_count, candidates, candidate_count, body_hashes = _python_tests(
                source.decode("utf-8", errors="ignore")
            )
            test_count += file_test_count
        elif PurePosixPath(path).suffix.lower() in {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}:
            try:
                text = source.decode("utf-8")
            except UnicodeDecodeError:
                javascript_source_decoding_error_paths.append(path)
                candidates, candidate_count = [], 0
            else:
                masked = _mask_javascript_non_code(text)
                declarations, scan_truncated = _javascript_test_scan(masked)
                if scan_truncated:
                    javascript_scan_truncated_paths.append(path)
                test_count += len(declarations)
                candidates, candidate_count = _test_candidates(
                    path,
                    source,
                    javascript_masked=masked,
                    javascript_declarations=declarations,
                )
            body_hashes = []
        else:
            candidates, candidate_count, body_hashes = [], 0, []
        raw_candidate_count += candidate_count
        source_sha256 = hashlib.sha256(source).hexdigest()
        for candidate in candidates:
            if len(raw_candidates) < _MAX_CANDIDATES:
                raw_candidates.append(
                    {
                        **candidate,
                        "path": path,
                        "evidence_sha256": source_sha256,
                    }
                )
        for line, fingerprint in body_hashes:
            previous = duplicate_bodies.get(fingerprint)
            if previous:
                previous_path, previous_line, previous_sha256 = previous
                raw_candidate_count += 1
                if len(raw_candidates) < _MAX_CANDIDATES:
                    raw_candidates.append(
                        {
                            "category": "duplicate_test_body",
                            "line": line,
                            "path": path,
                            "related": {
                                "path": previous_path,
                                "line": previous_line,
                                "evidence_sha256": previous_sha256,
                            },
                            "evidence_sha256": source_sha256,
                        }
                    )
            else:
                duplicate_bodies[fingerprint] = (path, line, source_sha256)
    raw_candidates.sort(key=lambda item: (item["path"], item["line"], item["category"]))
    candidates = []
    for item in raw_candidates[:_MAX_CANDIDATES]:
        related = item.get("related", {})
        related_key = f"{related.get('path', '')}:{related.get('line', '')}"
        candidates.append(
            {
                "candidate_id": _candidate_id(
                    item["category"], item["path"], item["line"], related_key
                ),
                **item,
            }
        )
    profile = _profile_for_snapshot(root, commit, objects, cache_dir=cache_dir, use_cache=use_cache)
    limitations = [
        "Candidates are static heuristics and require source review; they are not confirmed defects.",
        "Only committed Git blobs at the selected revision are scanned; dirty and untracked files are ignored.",
        "Critical-flow coverage is not inferred without an explicit flow inventory.",
        "Assertion, wait, skip, and duplicate-body checks currently cover Python and JavaScript/TypeScript.",
    ]
    return {
        "schema_version": _SCHEMA_VERSION,
        "repository": str(root),
        "revision": commit,
        "profile_fingerprint": profile["profile_fingerprint"],
        "test_files_total": len(paths),
        "test_files_scanned": len(sources),
        "selection_strategy": "changed_tests_then_related_basename_then_path_order",
        "test_files_skipped_by_limit_or_size": len(skipped)
        + max(0, len(paths) - len(selected_paths)),
        "tests_identified": test_count,
        "javascript_test_declaration_scan_truncated_paths": javascript_scan_truncated_paths,
        "javascript_source_decoding_error_paths": javascript_source_decoding_error_paths,
        "candidate_count": raw_candidate_count,
        "candidates": candidates,
        "candidates_omitted": max(0, raw_candidate_count - len(candidates)),
        "limitations": limitations,
    }


def repo_audit_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="qa-orch repo")
    subparsers = parser.add_subparsers(dest="command", required=True)
    profile = subparsers.add_parser(
        "profile", help="Profile tracked project, tests and CI metadata"
    )
    audit = subparsers.add_parser("audit-tests", help="Find bounded test-suite review candidates")
    for command in (profile, audit):
        command.add_argument("--repository", required=True, type=Path)
        command.add_argument("--revision", default="HEAD")
        command.add_argument("--no-cache", action="store_true")
        command.add_argument("--output", type=Path)
    audit.add_argument("--max-files", default=_MAX_AUDIT_FILES, type=int)
    audit.add_argument(
        "--changed-path",
        action="append",
        default=[],
        help="Prioritize this changed source/test path; may be repeated",
    )
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    if args.command == "profile":
        payload = build_repository_profile(
            args.repository,
            args.revision,
            cache_dir=settings.data_dir,
            use_cache=not args.no_cache,
        )
    else:
        payload = audit_test_suite(
            args.repository,
            args.revision,
            max_files=args.max_files,
            changed_paths=tuple(args.changed_path),
            cache_dir=settings.data_dir,
            use_cache=not args.no_cache,
        )
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0
