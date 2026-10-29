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
    MAX_REVIEW_SCENARIOS,
    EvidenceRecord,
    FinalReviewReport,
    ImpactCoverageLedger,
    ImpactCoverageOmission,
    ProfileReviewReport,
    ReviewAgent,
    ReviewContext,
    ReviewCriterionCoverage,
    ReviewDiffHunk,
    ReviewFindingCandidate,
    ReviewNote,
    ReviewScenarioDisposition,
    impact_candidate_set_sha256,
    impact_coverage_omissions_sha256,
    review_changed_paths_sha256,
    review_diff_hunk_inventory_sha256,
)
from qa_orchestrator.git_evidence import (
    _diff,
    _git,
    _is_text_source,
    _source_bytes,
    _source_lines,
    resolve_git_input,
)

_DISCOVERY_MAX_SYMBOLS = 128
_DISCOVERY_MAX_TRACKED_PATHS = 200_000
_DISCOVERY_MAX_SOURCE_BYTES = 4_000_000
_DISCOVERY_MAX_TOTAL_BYTES = 64_000_000
_DISCOVERY_MAX_CANDIDATES = 200
_DISCOVERY_MAX_DIFF_HUNKS = 500
_DISCOVERY_MAX_DIFF_ANCHOR_TERM_BYTES = 256
_DISCOVERY_MAX_CANDIDATE_SCAN_PATHS = 200
_DISCOVERY_MAX_SYMBOL_SEARCH_PATHS = 32
_DISCOVERY_MAX_SYMBOL_SEARCH_BYTES = 16_000_000
_DISCOVERY_MAX_MODULE_SEARCH_TERMS = 48
_DISCOVERY_MAX_MODULE_SEARCH_BYTES = 32_000_000
_DISCOVERY_MAX_RELATIONSHIP_EXPANSION_ROUNDS = 2
_DISCOVERY_MAX_RELATIONSHIP_EXPANSION_PATHS = 50
_DISCOVERY_MAX_RELATIONSHIP_EXPANSION_TERMS = 48
_DISCOVERY_MAX_CHANGED_FILE_CONTEXT_RANGES = 24
_DISCOVERY_MAX_CHANGED_FILE_CONTEXT_RANGES_PER_PATH = 6
_DISCOVERY_MAX_DIRECT_DIFF_ANCHOR_CONTEXT_RANGES = 24
_DISCOVERY_MAX_DIRECT_DIFF_ANCHOR_CONTEXT_RANGES_PER_PATH = 3
_DISCOVERY_MAX_DIRECT_DIFF_ANCHOR_MATCH_LINES_PER_PATH = 64
_DISCOVERY_MIN_SOURCE_BYTES_FOR_DIRECT_DIFF_ANCHOR_RANGE = 16_000
_DISCOVERY_DIRECT_DIFF_ANCHOR_CONTEXT_BEFORE = 5
_DISCOVERY_DIRECT_DIFF_ANCHOR_CONTEXT_AFTER = 20
_DISCOVERY_MAX_CONTROLLER_HANDLER_CONTEXT_LINES = 200
_DISCOVERY_MAX_CONTROLLER_HANDLER_CONTEXT_BYTES = 48_000
_DISCOVERY_MAX_CONTROLLER_ROUTE_PAIRS = 24
_DISCOVERY_MAX_DIRECT_CONTROLLER_IMPORTS = 24
_RELATIONSHIP_SOURCE_SUFFIXES = {
    ".c", ".cc", ".cpp", ".cs", ".go", ".h", ".hpp", ".java", ".js",
    ".jsx", ".kt", ".php", ".py", ".pyi", ".rb", ".rs", ".scala", ".swift",
    ".ts", ".tsx", ".vue",
}
_DISCOVERY_SYMBOL_SEARCH_BATCH_SIZE = 32
_DISCOVERY_MAX_OMISSION_EXAMPLES = 20
_CODEGRAPH_MAX_SYMBOL_QUERIES = 24
_CODEGRAPH_MAX_RELATION_CANDIDATES = 100
_IDENTIFIER = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]{2,127}$")
_IDENTIFIER_TOKEN = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]{2,127}")
_ADDED_CALL = re.compile(r"\b(?P<name>[A-Za-z_$][A-Za-z0-9_$]{2,127})\s*\(")
_ADDED_RECEIVER_CALL = re.compile(
    r"\b(?P<receiver>[A-Za-z_$][A-Za-z0-9_$]{2,127})\s*\.\s*"
    r"[A-Za-z_$][A-Za-z0-9_$]*\s*\("
)
_ADDED_ACTION_LITERAL = re.compile(
    r"\b[A-Za-z_$][A-Za-z0-9_$]*\s*\(\s*(['\"])(?P<name>[A-Za-z_$][A-Za-z0-9_$]{2,127})\1"
)
_ADDED_TYPE_REFERENCE = re.compile(
    r"\b(?:as|satisfies|implements|extends|instanceof)\s+"
    r"(?P<name>[A-Za-z_$][A-Za-z0-9_$]{2,127})\b"
)
_ROUTE_LITERAL = re.compile(r"[`'\"](?P<value>[^`'\"]*/[^`'\"]*)[`'\"]")
_DISPATCH_CALL = re.compile(
    r"\bdispatch\s*\((?P<arguments>[^;]{0,512}?)\)", re.DOTALL
)
_ACTION_ARGUMENT_TERMS = {
    "activate", "add", "clear", "close", "create", "deactivate", "delete",
    "disable", "enable", "hide", "mount", "open", "refresh", "remove", "reset",
    "show", "start", "stop", "toggle", "unmount", "update",
}
_COMMON_CALL_ANCHORS = {
    "afterEach", "beforeEach", "catch", "describe", "dispatch", "filter", "forEach",
    "it", "map", "mock", "reduce", "spyOn", "then", "toHaveBeenCalled",
    "toHaveBeenCalledTimes", "toHaveBeenCalledWith",
}
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
_RUBY_REQUIRE_REFERENCE = re.compile(
    r"^[ \t]*require(?P<relative>_relative)?[ \t]*(?:\([ \t]*)?"
    r"(?P<quote>['\"])(?P<module>[^'\"\r\n]+)(?P=quote)[ \t]*\)?[ \t]*(?:#.*)?$",
    re.MULTILINE,
)
_TEST_PATH = re.compile(
    r"(^|/)(tests?|specs?)(?:[-_.][^/]+)?(/|$)|(^|[._-])(test|spec)([._-]|$)",
    re.IGNORECASE,
)
_CONTRACT_PATH = re.compile(
    r"(^|/)(contracts?|schemas?|protocols?|types?|interfaces?|openapi|graphql|proto)(/|$)",
    re.IGNORECASE,
)
_API_PATH = re.compile(
    r"(^|/)(api|routes?|controllers?|endpoints?|graphql|openapi|proto)(/|$)", re.IGNORECASE
)
_CONTROLLER_PATH = re.compile(
    r"^(?P<prefix>.+)/controllers?/(?P<filename>[^/]+)$", re.IGNORECASE
)
_CONTROLLER_SOURCE_SUFFIXES = {".js", ".jsx", ".ts", ".tsx", ".vue"}
_CONTROLLER_HANDLER_DECLARATION = re.compile(
    r"^\s*(?:(?:export|default|async)\s+)*(?:function\s+[A-Za-z_$][\w$]*\s*\(|"
    r"(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*=.*=>)"
)
_VALIDATOR_PATH = re.compile(r"(^|/)(validation|validations|validator|validators)(/|$)", re.IGNORECASE)
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
    r"|(^|/)(?:Dockerfile(?:\..*)?|(?:docker-)?compose(?:\.[^.]+)?\.ya?ml)$",
    re.IGNORECASE,
)
_DOCKERFILE_PATH = re.compile(r"(^|/)Dockerfile(?:\.[^/]+)?$", re.IGNORECASE)
_COMPOSE_CONFIG_PATH = re.compile(
    r"(^|/)(?:docker-)?compose(?:\.[^/]+)?\.ya?ml$", re.IGNORECASE
)
_SERVICE_BOUNDARY_PATH = re.compile(
    r"(^|/)(services?|clients?|integrations?|adapters?)(/|$)", re.IGNORECASE
)
_COMMAND_ENTRYPOINT_PATH = re.compile(
    r"(^|/)cmd/[^/]+/main\.[A-Za-z0-9]+$", re.IGNORECASE
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
_TS_MODULE_REFERENCE = re.compile(
    r"\b(?:import|export)\s+(?P<bindings>[^;]*?)\s+from\s*['\"](?P<module>[^'\"]+)['\"]",
    re.DOTALL,
)
_TS_DYNAMIC_MODULE_REFERENCE = re.compile(
    r"\b(?:import|require)\s*\(\s*['\"](?P<module>[^'\"]+)['\"]\s*\)"
)
_TS_DECLARATION_SUFFIXES = (".d.ts", ".d.mts", ".d.cts")
_JS_TEST_MANIFESTS = {
    "package.json",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "yarn.lock",
}
_TEST_RUNNER_CONFIG_NAME = re.compile(
    r"^(?P<runner>playwright|cypress|jest|vitest|mocha|ava|karma)\.config\.[^.]+$",
    re.IGNORECASE,
)
_TEST_RUNNER_IMPORTS = {
    "playwright": re.compile(r"['\"](?:@playwright/test|playwright)['\"]"),
    "cypress": re.compile(r"['\"]cypress['\"]"),
    "jest": re.compile(r"['\"](?:jest|@jest/globals)['\"]"),
    "vitest": re.compile(r"['\"]vitest['\"]"),
    "mocha": re.compile(r"['\"]mocha['\"]"),
    "ava": re.compile(r"['\"]ava['\"]"),
    "karma": re.compile(r"['\"]karma[^'\"]*['\"]"),
}
_CI_WORKFLOW_PATH = re.compile(
    r"(^|/)\.github/workflows/[^/]+\.ya?ml$|(^|/)\.gitlab-ci\.ya?ml$|"
    r"(^|/)\.circleci/config\.ya?ml$|(^|/)azure-pipelines\.ya?ml$|"
    r"(^|/)bitbucket-pipelines\.ya?ml$",
    re.IGNORECASE,
)
_TEST_WORKFLOW_NAME = re.compile(r"(?:test|e2e|ci|check|verify)", re.IGNORECASE)
_TEST_WORKFLOW_COMMANDS = (
    re.compile(r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:test(?:[:_-][\w-]+)?|e2e)\b", re.IGNORECASE),
    re.compile(r"\b(?:npx|pnpm\s+exec|yarn\s+dlx)\s+(?:playwright|cypress|jest|vitest|mocha|ava)\b", re.IGNORECASE),
    re.compile(r"\b(?:python(?:3)?\s+-m\s+)?pytest(?:-\d+)?\b", re.IGNORECASE),
    re.compile(r"\bgo\s+test\b|\bcargo\s+test\b|\bmvn\s+(?:\S+\s+)*test\b|\bgradle\s+(?:\S+\s+)*test\b", re.IGNORECASE),
)
_DISCOVERY_MAX_TEST_RUNNER_CONFIG_CANDIDATES = 16
_DISCOVERY_MAX_TEST_WORKFLOW_SCAN_PATHS = 64
_DISCOVERY_MAX_TEST_WORKFLOW_SCAN_BYTES = 8_000_000
_DISCOVERY_MAX_TEST_WORKFLOW_SOURCE_BYTES = 1_000_000
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
) -> tuple[tuple[ReviewDiffHunk, ...], int]:
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
            if len(hunks) >= _DISCOVERY_MAX_DIFF_HUNKS:
                return tuple(hunks), _DISCOVERY_MAX_DIFF_HUNKS + 1
            hunk_bytes = b"".join(lines[index:end])
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
    return tuple(hunks), len(hunks)


def _diff_hunk_inventory(context: ReviewContext, diff: bytes) -> dict[str, object]:
    try:
        hunks, count_at_least = _parse_diff_hunks(context, diff)
    except (UnicodeDecodeError, ValueError):
        return {
            "status": "partial",
            "max_hunks": _DISCOVERY_MAX_DIFF_HUNKS,
            "count_at_least": None,
            "hunks": [],
            "inventory_sha256": None,
            "reason": "diff_hunk_parse_failed",
        }
    inventory_sha256 = review_diff_hunk_inventory_sha256(context.diff_sha256, hunks)
    if count_at_least > len(hunks):
        return {
            "status": "partial",
            "max_hunks": _DISCOVERY_MAX_DIFF_HUNKS,
            "count_at_least": count_at_least,
            "hunks": [hunk.model_dump(mode="json") for hunk in hunks],
            "inventory_sha256": inventory_sha256,
            "reason": "diff_hunk_limit_reached",
        }
    return {
        "status": "complete",
        "max_hunks": _DISCOVERY_MAX_DIFF_HUNKS,
        "count": count_at_least,
        "hunks": [hunk.model_dump(mode="json") for hunk in hunks],
        "inventory_sha256": inventory_sha256,
    }


def bind_diff_only(context: ReviewContext, diff: bytes) -> ReviewContext:
    """Bind a host-supplied unified diff without asserting Git provenance."""
    if context.input_kind != "diff":
        raise ValueError("diff-only binding requires diff input")
    if hashlib.sha256(diff).hexdigest() != context.diff_sha256:
        raise ValueError("diff hash does not match review context")
    hunks, count = _parse_diff_hunks(context, diff)
    if not hunks or count != len(hunks):
        raise ValueError("diff must contain a complete supported hunk inventory")
    chunks = re.split(rb"(?m)^(?=@@ )", diff)[1:]
    if len(chunks) != len(hunks):
        raise ValueError("invalid diff hunk structure")
    for hunk, chunk in zip(hunks, chunks, strict=True):
        old_count = new_count = 0
        for line in chunk.splitlines()[1:]:
            if line.startswith(b"diff --git "):
                break
            if line.startswith(b"\\ No newline at end of file"):
                continue
            if not line or line[:1] not in (b" ", b"+", b"-"):
                raise ValueError("invalid or truncated diff hunk")
            old_count += line[:1] in (b" ", b"-")
            new_count += line[:1] in (b" ", b"+")
        if (old_count, new_count) != (hunk.old_count, hunk.new_count):
            raise ValueError("truncated diff hunk")
    covered = {context.changed_files[h.changed_path_index] for h in hunks}
    if covered != set(context.changed_files):
        raise ValueError("diff hunks must cover all declared changed files")
    file_sections = re.split(rb"(?m)^diff --git ", diff)
    if file_sections[0].strip() or len(file_sections) - 1 != len(covered):
        raise ValueError("diff contains unsupported or unbound file changes")
    if any(b"\n@@ " not in section for section in file_sections[1:]):
        raise ValueError("diff contains unsupported or unbound file changes")
    return ReviewContext.model_validate(
        {
            **context.model_dump(),
            "diff_hunks": hunks,
            "diff_hunk_inventory_sha256": review_diff_hunk_inventory_sha256(
                context.diff_sha256, hunks
            ),
            "diff_hunk_inventory_status": "complete",
            "diff_hunk_inventory_count_at_least": len(hunks),
        }
    )


def _diff_relationship_terms(
    diff: bytes,
    referenced_symbols: set[str],
    *,
    change_marker: str,
    path_marker: str,
) -> tuple[tuple[str, ...], int]:
    """Extract bounded call, type, and domain-field anchors and count omitted terms."""
    try:
        lines = diff.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return (), 0
    action_terms = []
    semantic_terms = []
    semantic_term_set = set()
    call_terms = []
    changed_path_is_test = False

    def add_semantic_term(name: str) -> None:
        if name not in _IGNORED_IDENTIFIERS and name not in semantic_term_set:
            semantic_term_set.add(name)
            semantic_terms.append(name)

    for line in lines:
        if line.startswith(path_marker):
            changed_path_is_test = bool(_TEST_PATH.search(line[len(path_marker) :]))
            continue
        if not line.startswith(change_marker) or line.startswith(change_marker * 3):
            continue
        if changed_path_is_test:
            continue
        added = line[1:]
        for match in _ADDED_TYPE_REFERENCE.finditer(added):
            add_semantic_term(match.group("name"))
        for match in _IDENTIFIER_TOKEN.finditer(added):
            name = match.group()
            if "_" in name and len(name) >= 7 and not name.startswith("_"):
                add_semantic_term(name)
        action_terms.extend(
            match.group("name")
            for match in _ADDED_ACTION_LITERAL.finditer(added)
            if match.group("name").lower() in _ACTION_ARGUMENT_TERMS
        )
        for match in _ADDED_RECEIVER_CALL.finditer(added):
            receiver = match.group("receiver")
            mixed_case = any(char.isupper() for char in receiver[1:]) and any(
                char.islower() for char in receiver
            )
            if mixed_case:
                call_terms.append(receiver)
        for match in _ADDED_CALL.finditer(added):
            name = match.group("name")
            if name in _IGNORED_IDENTIFIERS or name in _COMMON_CALL_ANCHORS:
                continue
            if re.search(r"\b(?:function|def|class|interface|type)\s+$", added[:match.start()]):
                continue
            mixed_case = any(char.isupper() for char in name[1:]) and any(
                char.islower() for char in name
            )
            if name not in referenced_symbols and not mixed_case:
                continue
            call_terms.append(name)
    ordered_terms = tuple(dict.fromkeys((*action_terms, *semantic_terms, *call_terms)))
    eligible_terms = tuple(
        term
        for term in ordered_terms
        if len(term.encode("utf-8")) <= _DISCOVERY_MAX_DIFF_ANCHOR_TERM_BYTES
    )
    eligible_term_set = set(eligible_terms)
    if len(eligible_terms) <= _DISCOVERY_MAX_SYMBOLS:
        return eligible_terms, len(ordered_terms) - len(eligible_terms)

    action_terms = tuple(
        term for term in dict.fromkeys(action_terms) if term in eligible_term_set
    )
    semantic_terms = tuple(
        term for term in dict.fromkeys(semantic_terms) if term in eligible_term_set
    )
    call_terms = tuple(term for term in dict.fromkeys(call_terms) if term in eligible_term_set)
    selected = list(action_terms[:_DISCOVERY_MAX_SYMBOLS])
    remaining = _DISCOVERY_MAX_SYMBOLS - len(selected)
    call_quota = min(len(call_terms), remaining // 2)
    semantic_quota = min(len(semantic_terms), remaining - call_quota)
    selected.extend(call_terms[:call_quota])
    selected.extend(semantic_terms[:semantic_quota])
    selected_set = set(selected)
    for term in (*semantic_terms[semantic_quota:], *call_terms[call_quota:]):
        if len(selected) >= _DISCOVERY_MAX_SYMBOLS:
            break
        if term not in selected_set:
            selected.append(term)
            selected_set.add(term)
    return tuple(selected), len(ordered_terms) - len(selected)


def _candidate_route_terms(source: bytes) -> tuple[str, ...]:
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError:
        return ()
    route_terms = []
    for match in _ROUTE_LITERAL.finditer(text):
        value = match.group("value")
        if not value.startswith("/") or value.startswith("//"):
            continue
        route = value.split("${", 1)[0]
        route_terms.extend(_IDENTIFIER_TOKEN.findall(route))
    return tuple(dict.fromkeys(route_terms))


def _candidate_route_terms_near_anchors(
    source: bytes, anchors: Sequence[str], *, line_distance: int = 1
) -> tuple[str, ...]:
    """Extract route segments only where a route is near a directly matched anchor."""
    if not anchors:
        return ()
    try:
        lines = source.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return ()
    anchor_lines = [
        line_number
        for line_number, line in enumerate(lines, start=1)
        if _matching_relationship_terms(line.encode("utf-8"), anchors)
    ]
    route_terms = []
    for line_number, line in enumerate(lines, start=1):
        if not any(abs(line_number - anchor_line) <= line_distance for anchor_line in anchor_lines):
            continue
        for match in _ROUTE_LITERAL.finditer(line):
            value = match.group("value")
            if not value.startswith("/") or value.startswith("//"):
                continue
            route = value.split("${", 1)[0]
            route_terms.extend(_IDENTIFIER_TOKEN.findall(route))
    return tuple(dict.fromkeys(route_terms))


def _candidate_relationship_terms(source: bytes) -> tuple[str, ...]:
    """Extract bounded route and cross-file symbol anchors from a discovered source."""
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError:
        return ()

    route_terms = _candidate_route_terms(source)

    symbol_terms = []
    for name in _IDENTIFIER_TOKEN.findall(text):
        if name.lower() in _ACTION_ARGUMENT_TERMS:
            symbol_terms.append(name)
            continue
        mixed_case = any(char.isupper() for char in name[1:]) and any(
            char.islower() for char in name
        )
        snake_case = "_" in name and len(name) >= 7
        if name in _IGNORED_IDENTIFIERS or not (mixed_case or snake_case):
            continue
        symbol_terms.append(name)
    return tuple(dict.fromkeys((*route_terms, *symbol_terms)))


def _matching_relationship_terms(source: bytes, terms: Sequence[str]) -> set[str]:
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError:
        return set()
    return {
        term
        for term in terms
        if re.search(
            rf"(?<![A-Za-z0-9_$]){re.escape(term)}(?![A-Za-z0-9_$])",
            text,
        )
    }


def _dispatched_action_anchors(source: bytes, terms: Sequence[str]) -> tuple[str, ...]:
    actions = tuple(term for term in terms if term.lower() in _ACTION_ARGUMENT_TERMS)
    if not actions:
        return ()
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError:
        return ()
    dispatched = set()
    for match in _DISPATCH_CALL.finditer(text):
        arguments = match.group("arguments")
        dispatched.update(
            action
            for action in actions
            if re.search(rf"\.\s*{re.escape(action)}\s*\(", arguments)
        )
    return tuple(action for action in actions if action in dispatched)


def _relationship_expandable_source(path: str) -> bool:
    return (
        PurePosixPath(path).suffix.lower() in _RELATIONSHIP_SOURCE_SUFFIXES
        and not _TEST_PATH.search(path)
    )


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
    if not candidates:
        return
    strict_bound_review = (
        context.input_manifest is not None
        and context.input_manifest.schema_version == 2
    )
    if strict_bound_review and not context.evidence_registry:
        raise ValueError("strict review findings require registered evidence")
    if not context.evidence_registry:
        return
    registry = {entry.evidence_ref: entry for entry in context.evidence_registry}
    for candidate in candidates:
        cited_entries = [
            registry[ref] for ref in candidate.evidence_refs if ref in registry
        ]
        if strict_bound_review:
            if not candidate.source_locations:
                raise ValueError("strict review findings require source locations")
            if set(candidate.evidence_refs) - set(registry):
                raise ValueError("strict review finding cites unregistered evidence refs")
            if candidate.change_origin == "introduced" and not any(
                entry.kind == "diff"
                and entry.revision == context.head_sha
                and entry.sha256 == context.diff_sha256
                for entry in cited_entries
            ):
                raise ValueError("introduced finding requires bound diff evidence")
            if candidate.change_origin == "exacerbated_by_pr" and not any(
                entry.kind == "diff"
                and entry.revision == context.head_sha
                and entry.sha256 == context.diff_sha256
                for entry in cited_entries
            ):
                raise ValueError("exacerbated finding requires bound diff evidence")
        if context.input_kind == "diff":
            if not any(entry.kind == "diff" for entry in cited_entries):
                raise ValueError("diff-only findings require bound diff evidence")
            if candidate.change_origin in {"pre_existing", "exacerbated_by_pr"}:
                raise ValueError("diff-only input cannot establish a prior source state")
            for location in candidate.source_locations:
                if not any(
                    context.changed_files[h.changed_path_index] == location.path
                    and h.new_count > 0
                    and h.new_start <= location.start_line
                    and (location.end_line or location.start_line) < h.new_start + h.new_count
                    for h in context.diff_hunks
                ):
                    raise ValueError("diff-only finding location must be within a bound diff hunk")
            continue
        source_entries = [
            entry
            for entry in cited_entries
            if entry.kind == "source"
            and entry.revision == context.head_sha
            and (
                entry.source_text_available is True
                if strict_bound_review
                else entry.source_text_available is not False
            )
        ]
        base_source_entries = [
            entry
            for entry in cited_entries
            if entry.kind == "source"
            and entry.revision == context.base_sha
            and entry.source_text_available is True
        ]
        def supports_location(entry: EvidenceRecord, location) -> bool:
            return entry.path == location.path and (
                entry.start_line is None
                or (
                    location.start_line >= entry.start_line
                    and (location.end_line or location.start_line)
                    <= (entry.end_line or entry.start_line)
                )
            )

        for location in candidate.source_locations:
            if not any(
                supports_location(entry, location) for entry in source_entries
            ):
                raise ValueError(
                    "finding location is not supported by registered head source evidence"
                )
            if (
                strict_bound_review
                and candidate.change_origin in {"pre_existing", "exacerbated_by_pr"}
                and not any(
                    supports_location(entry, location) for entry in base_source_entries
                )
            ):
                if candidate.change_origin == "exacerbated_by_pr":
                    raise ValueError(
                        "exacerbated finding requires matching base source evidence"
                    )
                raise ValueError("pre-existing finding requires matching base source evidence")


def validate_context_selection(context: ReviewContext, report: ProfileReviewReport) -> None:
    scenario_refs = {
        ref for item in report.scenario_coverage for ref in item.evidence_refs
    }
    if scenario_refs - set(context.evidence_refs):
        raise ValueError("scenario coverage cites refs outside the bound context")
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
    cited.update(
        ref for item in report.scenario_coverage for ref in item.evidence_refs
    )
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
    if attempt.review_cycle_receipts:
        receipts = attempt.review_cycle_receipts
        expected_stages = ("review", "second_look", "verify") if report.finding_candidates else (
            "review", "second_look"
        )
        if tuple(receipt.stage for receipt in receipts) != expected_stages:
            return False
        if receipts[0].submitted_prompt_sha256 != attempt.submitted_prompt_sha256:
            return False
        for receipt in receipts:
            if (receipt.prompt_write_status != "written"
                    or receipt.source_packet_sha256 != context.evidence_packet_sha256):
                return False
            if receipt.stage == "verify":
                if (receipt.selected_packet_sha256 != context.evidence_packet_sha256
                        or set(receipt.included_evidence_refs) != set(context.evidence_refs)
                        or receipt.omitted_evidence_refs):
                    return False
            elif (receipt.selected_packet_sha256 != selection.selected_packet_sha256
                    or receipt.included_evidence_refs != selection.included_refs
                    or receipt.omitted_evidence_refs != selection.omitted_refs):
                return False
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
    if (
        context.neighbor_discovery_omissions_sha256 is not None
        and impact_coverage_omissions_sha256(ledger.omissions)
        != context.neighbor_discovery_omissions_sha256
    ):
        raise ValueError(
            "impact coverage omissions do not match bound neighbor discovery"
        )
    bound_candidate_omissions = context.neighbor_discovery_candidate_results_omitted_by_limit
    if bound_candidate_omissions is not None:
        ledger_candidate_omissions = sum(
            omission.count
            for omission in ledger.omissions
            if omission.kind == "candidate_result_limit"
        )
        if ledger_candidate_omissions != bound_candidate_omissions:
            raise ValueError(
                "impact coverage omissions do not match the bound candidate result limit"
            )
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
        validate_impact_coverage_context(
            report, context, require_ledger=context.input_kind == "git"
        )
    except ValueError as exc:
        gaps.append(str(exc))
    if report.impact_coverage is not None:
        status = impact_coverage_status(report.impact_coverage)
        if status != "closed":
            gaps.append(f"impact coverage status is {status}")
    omitted_candidates = context.neighbor_discovery_candidate_results_omitted_by_limit
    if omitted_candidates:
        label = "candidate" if omitted_candidates == 1 else "candidates"
        gaps.append(
            f"neighbor discovery result limit omitted {omitted_candidates} eligible {label}; "
            "rerun discovery with a higher limit before claiming complete impact coverage"
        )
    if context.diff_hunk_inventory_status == "partial":
        if context.diff_hunk_inventory_count_at_least is None:
            gaps.append(
                "diff hunk inventory is partial because parsing failed; no indexed hunks are available"
            )
        else:
            gaps.append(
                "diff hunk inventory is partial: at least "
                f"{context.diff_hunk_inventory_count_at_least} hunks exist, but only "
                f"{len(context.diff_hunks)} are indexed"
            )
    coverage = report.coverage_ledger
    if not (allow_legacy_missing_hunk_ledger and coverage is None):
        if context.diff_hunk_inventory_sha256 is None:
            if context.diff_hunk_inventory_status != "partial":
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


def complete_scenario_coverage(
    context: ReviewContext,
    profile_reports: Sequence[ProfileReviewReport],
    report: FinalReviewReport,
) -> FinalReviewReport:
    """Carry every prior material scenario into final synthesis or expose it as a gap."""
    packet_refs = set(context.evidence_refs)
    supplied_keys = {
        " ".join(item.scenario.split()).casefold() for item in report.scenario_coverage
    }
    for item in report.scenario_coverage:
        if set(item.evidence_refs) - packet_refs:
            raise ValueError("scenario coverage cites evidence outside the bound packet")

    missing: list[tuple[ReviewAgent, ReviewScenarioDisposition]] = []
    seen = set(supplied_keys)
    for profile_report in profile_reports:
        for item in profile_report.scenario_coverage:
            if set(item.evidence_refs) - packet_refs:
                raise ValueError("profile scenario coverage cites evidence outside the bound packet")
            key = " ".join(item.scenario.split()).casefold()
            if key not in seen:
                seen.add(key)
                missing.append((profile_report.profile, item))

    if not missing:
        return report

    scenarios = list(report.scenario_coverage)
    generated_gaps: list[ReviewNote] = []
    for profile, item in missing:
        if len(scenarios) < MAX_REVIEW_SCENARIOS:
            rationale = (
                "No final disposition was supplied; prior "
                f"{profile.value} status was {item.disposition}. {item.rationale}"
            )[:500]
            scenarios.append(
                ReviewScenarioDisposition(
                    scenario=item.scenario,
                    disposition="unverified",
                    evidence_refs=item.evidence_refs,
                    rationale=rationale,
                )
            )
        else:
            generated_gaps.append(
                ReviewNote(
                    text=(
                        f"{profile.value} left a material scenario without a final disposition: "
                        f"{item.scenario}"
                    ),
                    evidence_refs=item.evidence_refs,
                )
            )

    if len(report.coverage_gaps) + len(generated_gaps) > 100:
        raise ValueError("unrepresented prior scenarios exceed final report capacity")
    return FinalReviewReport.model_validate(
        {
            **report.model_dump(),
            "scenario_coverage": tuple(scenarios),
            "coverage_gaps": (*report.coverage_gaps, *generated_gaps),
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
        targets = [_normalized_relative_module_path(path, module)]
    elif module.startswith(("@/", "~/")):
        target = module[2:]
        if not any(
            f"{target}{suffix}" in tracked_paths
            for suffix in (".ts", ".tsx", ".js", ".jsx", *_TS_DECLARATION_SUFFIXES)
        ):
            target = f"src/{target}"
        targets = [target]
    elif module.startswith("~") and "/" in module[1:]:
        # Common tsconfig paths map `~name/*` to `src/name/*`; accept only tracked files.
        alias_target = module[1:]
        targets = [
            f"src/{alias_target}",
            f"web/src/{alias_target}",
            alias_target,
        ]
    elif module.startswith("src/"):
        targets = [module]
    else:
        return set()

    suffixes = (
        ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts",
        *_TS_DECLARATION_SUFFIXES, ".json",
    )
    resolved = set()
    for target in targets:
        variants = [target]
        if PurePosixPath(target).suffix.lower() in suffixes:
            suffix = PurePosixPath(target).suffix
            if suffix in {".js", ".jsx", ".mjs", ".cjs"}:
                stem = target[: -len(suffix)]
                variants.extend(
                    f"{stem}{replacement}"
                    for replacement in (".ts", ".tsx", ".mts", ".cts")
                )
        else:
            variants.extend(f"{target}{suffix}" for suffix in suffixes)
        variants.extend(
            f"{target}/index{suffix}" for suffix in suffixes if suffix != ".json"
        )
        resolved.update(candidate for candidate in variants if candidate in tracked_paths)
    return resolved


def _tracked_ruby_module_paths(
    path: str,
    module: str,
    *,
    relative: bool,
    tracked_paths: set[str],
) -> set[str]:
    """Resolve literal Ruby require paths without guessing dynamic load paths."""
    if not module or "#{" in module:
        return set()
    module_path = PurePosixPath(module)
    if module_path.is_absolute() or (not relative and module.startswith(".")):
        return set()

    parts = list(PurePosixPath(path).parent.parts) if relative else []
    for part in module_path.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not relative or not parts:
                return set()
            parts.pop()
            continue
        parts.append(part)
    target = "/".join(parts)
    if not target:
        return set()

    bases = [target]
    if not relative and not target.startswith("lib/"):
        bases.append(f"lib/{target}")
    candidates = set()
    for base in bases:
        if PurePosixPath(base).suffix.lower() == ".rb":
            candidates.add(base)
        else:
            candidates.add(f"{base}.rb")
    return candidates & tracked_paths


def _referenced_module_paths(path: str, source: bytes, tracked_paths: set[str]) -> set[str]:
    """Resolve supported static Python, Ruby, and JavaScript/TypeScript imports."""
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
        for reference_pattern in (_TS_MODULE_REFERENCE, _TS_DYNAMIC_MODULE_REFERENCE):
            for match in reference_pattern.finditer(text):
                paths.update(
                    _tracked_typescript_module_paths(path, match.group("module"), tracked_paths)
                )
        return paths
    if PurePosixPath(path).suffix.lower() == ".rb":
        paths = set()
        for match in _RUBY_REQUIRE_REFERENCE.finditer(text):
            paths.update(
                _tracked_ruby_module_paths(
                    path,
                    match.group("module"),
                    relative=match.group("relative") is not None,
                    tracked_paths=tracked_paths,
                )
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
        for match in _TS_MODULE_REFERENCE.finditer(text):
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


def _test_runner_config_paths(
    changed_paths: set[str],
    tracked_paths: set[str],
    changed_sources: Mapping[str, bytes],
    repository: Path,
    head_revision: str,
) -> tuple[list[str], list[str], list[str], int]:
    """Suggest nearest runner configs and named CI workflows for changed tests."""
    changed_tests = sorted(path for path in changed_paths if _TEST_PATH.search(path))
    if not changed_tests:
        return [], [], [], 0

    configs_by_runner: dict[str, list[str]] = {}
    for path in tracked_paths:
        match = _TEST_RUNNER_CONFIG_NAME.fullmatch(PurePosixPath(path).name)
        if match:
            configs_by_runner.setdefault(match.group("runner").lower(), []).append(path)

    ranked: dict[str, tuple[int, int, str]] = {}
    detected_runners_all: set[str] = set()
    for test_path in changed_tests:
        source = changed_sources.get(test_path, b"").decode("utf-8", errors="ignore")
        detected_runners = {
            runner
            for runner, expression in _TEST_RUNNER_IMPORTS.items()
            if expression.search(source)
        }
        detected_runners_all.update(detected_runners)
        runner_names = detected_runners or set(configs_by_runner)
        test_parent = PurePosixPath(test_path).parent.parts
        for runner in runner_names:
            matching_configs = [
                config
                for config in configs_by_runner.get(runner, ())
                if len(PurePosixPath(config).parent.parts) <= len(test_parent)
                and test_parent[: len(PurePosixPath(config).parent.parts)]
                == PurePosixPath(config).parent.parts
            ]
            if matching_configs:
                nearest = min(
                    matching_configs,
                    key=lambda config: (
                        -len(PurePosixPath(config).parent.parts),
                        config,
                    ),
                )
                ranked[nearest] = (
                    0,
                    -len(PurePosixPath(nearest).parent.parts),
                    nearest,
                )

    workflow_candidates = []
    for path in sorted(tracked_paths):
        if not _CI_WORKFLOW_PATH.search(path):
            continue
        name = PurePosixPath(path).name
        runner_named = any(runner in name.lower() for runner in detected_runners_all)
        if (
            name.lower() != ".gitlab-ci.yml"
            and not _TEST_WORKFLOW_NAME.search(name)
            and not runner_named
        ):
            continue
        workflow_candidates.append(path)

    scan_omitted = workflow_candidates[_DISCOVERY_MAX_TEST_WORKFLOW_SCAN_PATHS:]
    scanned_bytes = 0
    workflow_paths = []
    workflow_paths_scanned = 0
    for path in workflow_candidates[:_DISCOVERY_MAX_TEST_WORKFLOW_SCAN_PATHS]:
        workflow_paths_scanned += 1
        size = int(_git(repository, "cat-file", "-s", f"{head_revision}:{path}"))
        if (
            size > _DISCOVERY_MAX_TEST_WORKFLOW_SOURCE_BYTES
            or scanned_bytes + size > _DISCOVERY_MAX_TEST_WORKFLOW_SCAN_BYTES
        ):
            scan_omitted.append(path)
            continue
        source = _git(repository, "show", f"{head_revision}:{path}")
        scanned_bytes += len(source)
        text = source.decode("utf-8", errors="ignore")
        if not any(expression.search(text) for expression in _TEST_WORKFLOW_COMMANDS):
            continue
        workflow_paths.append(path)

    for path in workflow_paths:
        name = PurePosixPath(path).name
        test_named = bool(re.search(r"test|e2e", name, re.IGNORECASE))
        ranked[path] = (1 if test_named else 2, 0, path)

    ordered = sorted(ranked, key=lambda path: ranked[path])
    limit = _DISCOVERY_MAX_TEST_RUNNER_CONFIG_CANDIDATES
    return ordered[:limit], ordered[limit:], scan_omitted, workflow_paths_scanned


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
    candidate_result_fields = (
        "candidate_limit",
        "candidate_results_considered",
        "candidate_results_omitted_by_limit",
    )
    bound_candidate_results_omitted = None
    bound_discovery_omissions = None
    bound_discovery_omissions_sha256 = None
    if "omissions" in payload:
        raw_discovery_omissions = payload["omissions"]
        if not isinstance(raw_discovery_omissions, list):
            raise TypeError("neighbor discovery omissions must be a list")
        bound_discovery_omissions = []
        for omission in raw_discovery_omissions:
            if not isinstance(omission, Mapping):
                raise TypeError("neighbor discovery omission record is invalid")
            bound_discovery_omissions.append(
                ImpactCoverageOmission.model_validate(
                    {
                        key: omission[key]
                        for key in ("kind", "count", "limit", "examples")
                        if key in omission
                    }
                )
            )
        bound_discovery_omissions_sha256 = impact_coverage_omissions_sha256(
            tuple(bound_discovery_omissions)
        )
    if any(field in payload for field in candidate_result_fields):
        if not all(field in payload for field in candidate_result_fields):
            raise ValueError("neighbor discovery candidate result limits are incomplete")
        candidate_limit = payload["candidate_limit"]
        results_considered = payload["candidate_results_considered"]
        results_omitted = payload["candidate_results_omitted_by_limit"]
        if (
            type(candidate_limit) is not int
            or candidate_limit < 0
            or type(results_considered) is not int
            or results_considered < 0
            or type(results_omitted) is not int
            or results_omitted < 0
            or len(candidate_manifest) != min(results_considered, candidate_limit)
            or results_omitted != max(0, results_considered - candidate_limit)
        ):
            raise ValueError("neighbor discovery candidate result limits are inconsistent")
        if bound_discovery_omissions is None:
            raise TypeError("neighbor discovery omissions must be a list")
        result_limit_omissions = [
            omission
            for omission in bound_discovery_omissions
            if omission.kind == "candidate_result_limit"
        ]
        if results_omitted:
            if (
                len(result_limit_omissions) != 1
                or result_limit_omissions[0].count != results_omitted
                or result_limit_omissions[0].limit != candidate_limit
            ):
                raise ValueError(
                    "neighbor discovery candidate result omission does not match its limit"
                )
        elif result_limit_omissions:
            raise ValueError(
                "neighbor discovery candidate result omission is unexpected"
            )
        bound_candidate_results_omitted = results_omitted
    inventory = payload.get("diff_hunk_inventory")
    if not isinstance(inventory, Mapping):
        raise TypeError("neighbor discovery diff hunk inventory must be an object")
    bound_hunks: tuple[ReviewDiffHunk, ...] = ()
    bound_hunk_digest = None
    bound_hunk_count_at_least = None
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
        bound_hunk_count_at_least = len(bound_hunks)
    elif inventory.get("status") == "partial":
        raw_hunks = inventory.get("hunks")
        raw_count_at_least = inventory.get("count_at_least")
        reason = inventory.get("reason")
        if not isinstance(raw_hunks, list) or reason not in {
            "diff_hunk_limit_reached",
            "diff_hunk_parse_failed",
        }:
            raise ValueError("partial diff hunk inventory is malformed")
        if raw_count_at_least is not None and (
            type(raw_count_at_least) is not int or raw_count_at_least < 1
        ):
            raise ValueError("partial diff hunk count is invalid")
        if raw_hunks:
            if (
                reason != "diff_hunk_limit_reached"
                or inventory.get("max_hunks") != _DISCOVERY_MAX_DIFF_HUNKS
                or len(raw_hunks) != _DISCOVERY_MAX_DIFF_HUNKS
                or type(raw_count_at_least) is not int
                or raw_count_at_least <= len(raw_hunks)
            ):
                raise ValueError("partial diff hunk prefix has inconsistent limits")
            bound_hunks = tuple(ReviewDiffHunk.model_validate(item) for item in raw_hunks)
            bound_hunk_digest = review_diff_hunk_inventory_sha256(
                context.diff_sha256, bound_hunks
            )
            if inventory.get("inventory_sha256") != bound_hunk_digest:
                raise ValueError("partial diff hunk prefix digest is invalid")
        elif inventory.get("inventory_sha256") is not None:
            raise ValueError("empty partial diff hunk inventory cannot have an index digest")
        if reason == "diff_hunk_parse_failed" and raw_count_at_least is not None:
            raise ValueError("unparsed diff hunk inventory cannot claim a count")
        if reason == "diff_hunk_limit_reached" and (
            type(raw_count_at_least) is not int
            or raw_count_at_least <= _DISCOVERY_MAX_DIFF_HUNKS
        ):
            raise ValueError("limited diff hunk inventory count is inconsistent")
        bound_hunk_count_at_least = raw_count_at_least
    else:
        raise ValueError("neighbor discovery diff hunk inventory status is invalid")
    return ReviewContext.model_validate(
        {
            **context.model_dump(mode="json"),
            "neighbor_discovery_sha256": payload["discovery_sha256"],
            "neighbor_discovery_candidate_count": len(candidate_manifest),
            "neighbor_discovery_candidate_set_sha256": candidate_digest,
            "neighbor_discovery_candidate_results_omitted_by_limit": (
                bound_candidate_results_omitted
            ),
            "neighbor_discovery_omissions_sha256": bound_discovery_omissions_sha256,
            "diff_hunks": [hunk.model_dump(mode="json") for hunk in bound_hunks],
            "diff_hunk_inventory_sha256": bound_hunk_digest,
            "diff_hunk_inventory_status": inventory["status"],
            "diff_hunk_inventory_count_at_least": bound_hunk_count_at_least,
        }
    )


def _neighbor_categories(path: str) -> tuple[str, ...]:
    tags = []
    if _TEST_PATH.search(path):
        tags.append("test")
    if _TEST_RUNNER_CONFIG_NAME.fullmatch(PurePosixPath(path).name):
        tags.append("test_runner_config")
    if _CI_WORKFLOW_PATH.search(path) and _TEST_WORKFLOW_NAME.search(PurePosixPath(path).name):
        tags.append("test_runner_config")
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
    if _SERVICE_BOUNDARY_PATH.search(path) or _COMMAND_ENTRYPOINT_PATH.search(path):
        tags.append("service_boundary")
    return tuple(tags or ("caller",))


def _related_container_config_paths(
    changed_paths: set[str], tracked_paths: set[str]
) -> set[str]:
    """Pair changed container build files with tracked Compose runtime configuration."""
    changed_dockerfile = any(_DOCKERFILE_PATH.search(path) for path in changed_paths)
    changed_compose = any(_COMPOSE_CONFIG_PATH.search(path) for path in changed_paths)
    if not changed_dockerfile and not changed_compose:
        return set()
    return {
        path
        for path in tracked_paths - changed_paths
        if (changed_dockerfile and _COMPOSE_CONFIG_PATH.search(path))
        or (changed_compose and _DOCKERFILE_PATH.search(path))
    }


def _path_tokens(path: str) -> set[str]:
    path_without_suffix = PurePosixPath(path).with_suffix("").as_posix()
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", path_without_suffix)
    return {
        part.lower()
        for part in re.findall(r"[A-Za-z0-9]+", separated)
        if len(part) >= 3 and part.lower() not in _IGNORED_PATH_TOKENS
    }


def _controller_handler_start_line(
    path: str, source_lines: list[str], anchor_line: int
) -> int | None:
    if (
        not _CONTROLLER_PATH.match(path)
        or PurePosixPath(path).suffix.lower() not in _CONTROLLER_SOURCE_SUFFIXES
    ):
        return None
    anchor_indent = len(source_lines[anchor_line - 1]) - len(
        source_lines[anchor_line - 1].lstrip()
    )
    earliest_line = max(
        1,
        anchor_line - _DISCOVERY_MAX_CONTROLLER_HANDLER_CONTEXT_LINES + 1,
    )
    for line_number in range(anchor_line, earliest_line - 1, -1):
        line = source_lines[line_number - 1]
        if not _CONTROLLER_HANDLER_DECLARATION.match(line):
            continue
        declaration_indent = len(line) - len(line.lstrip())
        if declaration_indent < anchor_indent:
            return line_number
    return None


def _controller_handler_range(
    path: str, source_lines: list[str], anchor_line: int
) -> tuple[int, int] | None:
    """Return a bounded, indentation-matched block handler containing an anchor."""
    start_line = _controller_handler_start_line(path, source_lines, anchor_line)
    if start_line is None:
        return None
    declaration_indent = len(source_lines[start_line - 1]) - len(
        source_lines[start_line - 1].lstrip()
    )
    last_line = min(
        len(source_lines),
        start_line + _DISCOVERY_MAX_CONTROLLER_HANDLER_CONTEXT_LINES - 1,
    )
    for line_number in range(anchor_line, last_line + 1):
        line = source_lines[line_number - 1]
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if (
            indent == declaration_indent
            and re.fullmatch(r"}\s*;?\s*(?://.*)?", stripped)
        ):
            return start_line, line_number
        if (
            line_number > anchor_line
            and indent <= declaration_indent
            and _CONTROLLER_HANDLER_DECLARATION.match(line)
        ):
            return None
    return None


def _typescript_import_local_names(bindings: str) -> set[str]:
    """Return local binding names from a static TypeScript import clause."""
    clause = bindings.strip()
    if clause.startswith("type "):
        clause = clause[5:].strip()
    names = set()
    namespace = re.search(r"\*\s+as\s+([A-Za-z_$][A-Za-z0-9_$]*)", clause)
    if namespace:
        names.add(namespace.group(1))
    named_imports = re.search(r"\{(?P<items>[^}]*)\}", clause)
    if named_imports:
        for item in named_imports.group("items").split(","):
            item = re.sub(r"^\s*type\s+", "", item).strip()
            if not item:
                continue
            local_name = re.split(r"\s+as\s+", item)[-1].strip()
            if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", local_name):
                names.add(local_name)
        default_clause = clause[: named_imports.start()].strip().rstrip(",").strip()
        if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", default_clause):
            names.add(default_clause)
    elif not namespace and re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", clause):
        names.add(clause)
    return names


def _direct_controller_handler_import_references(
    path: str,
    source: bytes,
    tracked_paths: set[str],
    anchor_terms: Sequence[str],
) -> dict[str, dict[str, set[str]]]:
    """Resolve imports referenced in a bounded handler containing a changed anchor."""
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError:
        return {}
    lines = text.splitlines()
    valid_terms = [term for term in anchor_terms if _IDENTIFIER.fullmatch(term)]
    if not lines or not valid_terms:
        return {}
    term_pattern = re.compile(
        rf"(?<![A-Za-z0-9_$])(?:{'|'.join(re.escape(term) for term in valid_terms)})(?![A-Za-z0-9_$])"
    )
    imports_by_path: dict[str, set[str]] = {}
    for match in _TS_MODULE_REFERENCE.finditer(text):
        if not match.group(0).lstrip().startswith("import"):
            continue
        local_names = _typescript_import_local_names(match.group("bindings"))
        if not local_names:
            continue
        for imported_path in _tracked_typescript_module_paths(
            path, match.group("module"), tracked_paths
        ):
            imports_by_path.setdefault(imported_path, set()).update(local_names)
    if not imports_by_path:
        return {}

    references: dict[str, dict[str, set[str]]] = {}
    for anchor_line, line in enumerate(lines, 1):
        if not term_pattern.search(line):
            continue
        handler_range = _controller_handler_range(path, lines, anchor_line)
        if handler_range is None:
            continue
        handler_start, handler_end = handler_range
        if (
            handler_end - handler_start + 1
            > _DISCOVERY_MAX_CONTROLLER_HANDLER_CONTEXT_LINES
        ):
            continue
        handler_source = "\n".join(lines[handler_start - 1 : handler_end])
        if len(handler_source.encode("utf-8")) > _DISCOVERY_MAX_CONTROLLER_HANDLER_CONTEXT_BYTES:
            continue
        for imported_path, local_names in imports_by_path.items():
            used_names = {
                name
                for name in local_names
                if re.search(
                    rf"(?<![A-Za-z0-9_$]){re.escape(name)}(?![A-Za-z0-9_$])",
                    handler_source,
                )
            }
            if used_names:
                called_names = {
                    name
                    for name in used_names
                    if re.search(
                        rf"(?<![A-Za-z0-9_$]){re.escape(name)}\s*"
                        rf"(?:\.\s*[A-Za-z_$][A-Za-z0-9_$]*\s*)?\(",
                        handler_source,
                    )
                }
                reference = references.setdefault(
                    imported_path,
                    {"symbols": set(), "called_symbols": set()},
                )
                reference["symbols"].update(used_names)
                reference["called_symbols"].update(called_names)
    return references


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
    index_extraction_current = (
        isinstance(index_data, dict)
        and index_data.get("reindexRecommended") is False
        and type(index_data.get("builtWithExtractionVersion")) is int
        and type(index_data.get("currentExtractionVersion")) is int
        and index_data["builtWithExtractionVersion"]
        == index_data["currentExtractionVersion"]
    )
    if (
        not same_project
        or not pending_clean
        or index_status.get("worktreeMismatch", "missing") is not None
        or not index_complete
        or not index_extraction_current
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
    use_codegraph: bool = True,
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
    changed_sources: dict[str, bytes] = {}
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
        changed_sources[path] = source
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
    added_diff_terms, added_diff_terms_omitted = _diff_relationship_terms(
        pinned_diff,
        set(ordered_referenced_symbols),
        change_marker="+",
        path_marker="+++ b/",
    )
    removed_diff_terms, removed_diff_terms_omitted = _diff_relationship_terms(
        pinned_diff,
        set(ordered_referenced_symbols),
        change_marker="-",
        path_marker="--- a/",
    )
    changed_diff_terms = tuple(dict.fromkeys((*added_diff_terms, *removed_diff_terms)))
    all_module_search_terms = tuple(
        dict.fromkeys(
            (
                *changed_diff_terms,
                *module_names,
                *ordered_referenced_symbols,
                *ordered_defined_symbols,
            )
        )
    )
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
    (
        test_runner_config_paths,
        test_runner_config_paths_omitted,
        test_workflow_scan_omitted,
        test_workflow_paths_scanned,
    ) = _test_runner_config_paths(
        changed,
        tracked_set,
        changed_sources,
        repository,
        context.head_sha,
    )
    related_container_config_paths = _related_container_config_paths(changed, tracked_set)

    test_paths = set()
    ranked_symbol_paths = []
    for path in tracked:
        path_tokens = _path_tokens(path)
        if _TEST_PATH.search(path) and (path_tokens & changed_path_tokens):
            test_paths.add(path)
        if path in changed or (
            not ordered_symbols
            and path not in referenced_module_paths
            and path not in related_container_config_paths
        ):
            continue
        changed_matches = path_tokens & changed_path_tokens
        symbol_matches = path_tokens & symbol_path_tokens
        same_directory = PurePosixPath(path).parent in changed_directories
        if not (
            path in referenced_module_paths
            or path in related_container_config_paths
            or changed_matches
            or symbol_matches
            or same_directory
        ):
            continue
        categories = _neighbor_categories(path)
        priority = (
            0 if path in referenced_module_paths else
            1 if path in related_container_config_paths else
            2 if path in codegraph_by_path or path in codegraph_callees_by_path else
            3 if "test" in categories and changed_matches else
            4 if set(categories) & _SYSTEM_BOUNDARY_CATEGORIES
            and (changed_matches or symbol_matches) else
            5 if symbol_matches else
            6 if same_directory else 7
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

    direct_anchor_search_paths: set[str] = set()
    direct_anchor_search_bytes = 0
    if changed_diff_terms:
        command = ["git", "-C", str(repository), "grep", "-I", "-F", "-w", "-l", "-z", "-i"]
        for term in changed_diff_terms:
            command.extend(("-e", term))
        command.extend((context.head_sha, "--"))
        result = subprocess.run(command, capture_output=True, check=False)
        if result.returncode not in {0, 1}:
            raise ValueError("pinned changed-anchor search failed")
        direct_anchor_search_bytes = len(result.stdout)
        if direct_anchor_search_bytes > _DISCOVERY_MAX_MODULE_SEARCH_BYTES:
            raise ValueError("changed-anchor search result exceeds the output limit")
        try:
            direct_anchor_search_paths.update(
                part.decode("utf-8").split(":", 1)[-1]
                for part in result.stdout.split(b"\0")
                if part
            )
        except UnicodeDecodeError as exc:
            raise ValueError("changed-anchor paths must be valid UTF-8") from exc

    candidate_source_cache: dict[str, bytes] = {}
    direct_controller_candidates = tuple(
        path
        for path in sorted(direct_anchor_search_paths)
        if _CONTROLLER_PATH.match(path)
    )
    direct_controller_paths = direct_controller_candidates[
        :_DISCOVERY_MAX_CONTROLLER_ROUTE_PAIRS
    ]
    direct_controller_paths_omitted = max(
        0,
        len(direct_controller_candidates) - len(direct_controller_paths),
    )
    paired_controller_route_candidates = set()
    for controller_path in direct_controller_paths:
        match = _CONTROLLER_PATH.match(controller_path)
        if match is None:
            continue
        route_path = f"{match.group('prefix')}/routes/{match.group('filename')}"
        if route_path in tracked_set and route_path not in changed:
            paired_controller_route_candidates.add(route_path)
    paired_controller_route_paths = tuple(
        sorted(paired_controller_route_candidates)[:_DISCOVERY_MAX_CONTROLLER_ROUTE_PAIRS]
    )
    paired_controller_route_paths_omitted = max(
        0,
        len(paired_controller_route_candidates) - len(paired_controller_route_paths),
    )
    controller_context_paths = set(direct_controller_paths) | set(
        paired_controller_route_paths
    )
    direct_controller_import_candidates = set()
    direct_controller_import_references_by_path: dict[
        str, dict[str, dict[str, set[str]]]
    ] = {}
    for path in sorted(controller_context_paths):
        size = int(_git(repository, "cat-file", "-s", f"{context.head_sha}:{path}"))
        if (
            size > _DISCOVERY_MAX_SOURCE_BYTES
            or total_bytes + size > _DISCOVERY_MAX_TOTAL_BYTES
        ):
            continue
        source = _git(repository, "show", f"{context.head_sha}:{path}")
        candidate_source_cache[path] = source
        total_bytes += len(source)
        scanned_sources += 1
        direct_controller_import_candidates.update(
            _referenced_module_paths(path, source, tracked_set) - changed
        )
        if path in direct_controller_paths:
            for imported_path, references in _direct_controller_handler_import_references(
                path, source, tracked_set, changed_diff_terms
            ).items():
                controller_references = direct_controller_import_references_by_path.setdefault(
                    imported_path, {}
                ).setdefault(
                    path,
                    {"symbols": set(), "called_symbols": set()},
                )
                for key in ("symbols", "called_symbols"):
                    controller_references[key].update(references[key])
    direct_controller_import_candidates -= controller_context_paths
    direct_controller_import_candidates = {
        path
        for path in direct_controller_import_candidates
        if path in tracked_set
    }
    direct_controller_import_paths = tuple(
        sorted(
            direct_controller_import_candidates,
            key=lambda path: (
                0 if "validator" in path.lower() or "validation" in path.lower() else
                1 if "/middleware/" in path.lower() else
                2 if any(
                    reference["called_symbols"]
                    for reference in direct_controller_import_references_by_path.get(path, {}).values()
                ) else
                3 if path in direct_controller_import_references_by_path else
                4 if "/utils/" in path.lower() else 5,
                -len(_path_tokens(path) & changed_path_tokens),
                path,
            ),
        )[:_DISCOVERY_MAX_DIRECT_CONTROLLER_IMPORTS]
    )
    direct_controller_import_paths_omitted = max(
        0,
        len(direct_controller_import_candidates) - len(direct_controller_import_paths),
    )

    seed_candidate_paths = (
        grep_paths
        | module_name_search_paths
        | direct_anchor_search_paths
        | referenced_module_paths
        | test_paths
        | dependency_manifest_paths
        | set(test_runner_config_paths)
        | related_container_config_paths
        | set(paired_controller_route_paths)
        | set(direct_controller_import_paths)
        | set(codegraph_by_path)
        | set(codegraph_callees_by_path)
    ) - changed
    relationship_depth_by_path = {path: 0 for path in seed_candidate_paths}
    changed_file_relationship_terms_by_path: dict[str, set[str]] = {}
    changed_file_route_terms_by_path: dict[str, set[str]] = {}
    relationship_route_anchor_terms: set[str] = set()
    for path in direct_anchor_search_paths & changed:
        source = changed_sources.get(path)
        if source is not None:
            matched_terms = _matching_relationship_terms(source, changed_diff_terms)
            if matched_terms:
                changed_file_relationship_terms_by_path[path] = matched_terms
                relationship_depth_by_path[path] = 0
                local_route_terms = set(
                    _candidate_route_terms_near_anchors(source, tuple(matched_terms))
                )
                if local_route_terms:
                    changed_file_route_terms_by_path[path] = local_route_terms
                    relationship_route_anchor_terms.update(local_route_terms)
    relationship_expansion_paths: set[str] = set()
    relationship_expansion_terms: list[str] = []
    relationship_expansion_terms_searched: set[str] = set(module_search_terms)
    relationship_expansion_sources_considered = 0
    relationship_expansion_sources_read = 0
    relationship_expansion_frontier_omitted = 0
    relationship_expansion_frontier_omitted_examples = []
    relationship_expansion_terms_omitted = 0
    relationship_expansion_term_omitted_examples = []
    relationship_expansion_source_paths_skipped_by_size = []
    relationship_expansion_search_bytes = 0

    def expansion_path_priority(path: str) -> tuple:
        categories = _neighbor_categories(path)
        path_overlap = len(_path_tokens(path) & changed_path_tokens)
        return (
            0 if path in changed_sources else
            1 if path in direct_anchor_search_paths else
            2 if path in referenced_module_paths else
            3 if path in codegraph_by_path or path in codegraph_callees_by_path else
            4 if set(categories) & _SYSTEM_BOUNDARY_CATEGORIES else
            5 if path_overlap else
            6 if path in module_name_search_paths else
            7 if path in test_paths else 8,
            -path_overlap,
            path,
        )

    expansion_seed_paths = (
        direct_anchor_search_paths
        | referenced_module_paths
        | set(codegraph_by_path)
        | set(codegraph_callees_by_path)
    )
    expansion_seed_paths = {
        path for path in expansion_seed_paths if _relationship_expandable_source(path)
    }
    frontier = sorted(expansion_seed_paths, key=expansion_path_priority)
    expanded_source_paths: set[str] = set(frontier)
    expansion_path_limit = min(
        _DISCOVERY_MAX_RELATIONSHIP_EXPANSION_PATHS,
        max(1, max_candidates),
    )
    for expansion_depth in range(1, _DISCOVERY_MAX_RELATIONSHIP_EXPANSION_ROUNDS + 1):
        relationship_expansion_sources_considered += len(frontier)
        relationship_expansion_frontier_omitted += max(0, len(frontier) - expansion_path_limit)
        relationship_expansion_frontier_omitted_examples.extend(
            frontier[expansion_path_limit:]
        )
        expansion_sources = frontier[:expansion_path_limit]
        source_terms_for_round = []
        route_term_sources: dict[str, set[str]] = {}
        for path in expansion_sources:
            if path in changed_sources:
                source = changed_sources[path]
            elif path not in candidate_source_cache:
                size = int(_git(repository, "cat-file", "-s", f"{context.head_sha}:{path}"))
                if (
                    size > _DISCOVERY_MAX_SOURCE_BYTES
                    or total_bytes + symbol_search_bytes + size > _DISCOVERY_MAX_TOTAL_BYTES
                ):
                    skipped_by_limits += 1
                    relationship_expansion_source_paths_skipped_by_size.append(path)
                    continue
                candidate_source_cache[path] = _git(
                    repository, "show", f"{context.head_sha}:{path}"
                )
                total_bytes += len(candidate_source_cache[path])
                scanned_sources += 1
                source = candidate_source_cache[path]
            else:
                source = candidate_source_cache[path]
            relationship_expansion_sources_read += 1
            source_terms_for_round.append(
                (path, _candidate_relationship_terms(source)[
                    :_DISCOVERY_MAX_RELATIONSHIP_EXPANSION_TERMS
                ])
            )
            for term in _candidate_route_terms(source):
                route_term_sources.setdefault(term, set()).add(path)
        shared_route_terms = sorted(
            (
                term
                for term, paths in route_term_sources.items()
                if len(term) >= 4 and len(paths) > 1
            ),
            key=lambda term: (
                -len(route_term_sources[term]),
                -sum(path in changed_sources for path in route_term_sources[term]),
                term,
            ),
        )
        round_robin_terms = []
        for term_index in range(
            max((len(terms) for _, terms in source_terms_for_round), default=0)
        ):
            for _, source_terms in source_terms_for_round:
                if term_index < len(source_terms):
                    round_robin_terms.append(source_terms[term_index])
        deduplicated_terms = tuple(
            dict.fromkeys((*shared_route_terms, *round_robin_terms))
        )
        new_terms = tuple(
            term
            for term in deduplicated_terms
            if term not in relationship_expansion_terms_searched
        )
        terms = new_terms[:_DISCOVERY_MAX_RELATIONSHIP_EXPANSION_TERMS]
        relationship_expansion_terms_omitted += max(0, len(new_terms) - len(terms))
        relationship_expansion_term_omitted_examples.extend(
            new_terms[_DISCOVERY_MAX_RELATIONSHIP_EXPANSION_TERMS:]
        )
        relationship_expansion_terms.extend(terms)
        relationship_expansion_terms_searched.update(terms)
        if not terms:
            frontier = []
            continue

        command = ["git", "-C", str(repository), "grep", "-I", "-F", "-w", "-l", "-z", "-i"]
        for term in terms:
            command.extend(("-e", term))
        command.extend((context.head_sha, "--"))
        result = subprocess.run(command, capture_output=True, check=False)
        if result.returncode not in {0, 1}:
            raise ValueError("pinned relationship expansion search failed")
        relationship_expansion_search_bytes += len(result.stdout)
        if relationship_expansion_search_bytes > _DISCOVERY_MAX_MODULE_SEARCH_BYTES:
            raise ValueError("relationship expansion result exceeds the output limit")
        try:
            found_paths = {
                part.decode("utf-8").split(":", 1)[-1]
                for part in result.stdout.split(b"\0")
                if part
            }
        except UnicodeDecodeError as exc:
            raise ValueError("relationship expansion paths must be valid UTF-8") from exc
        frontier = []
        for path in sorted(found_paths):
            if not _relationship_expandable_source(path):
                continue
            if path in changed:
                source = changed_sources.get(path)
                if source is None:
                    continue
                matched_terms = _matching_relationship_terms(source, terms)
                if matched_terms:
                    changed_file_relationship_terms_by_path.setdefault(path, set()).update(
                        matched_terms
                    )
                    matched_route_terms = matched_terms & relationship_route_anchor_terms
                    if matched_route_terms:
                        changed_file_route_terms_by_path.setdefault(path, set()).update(
                            matched_route_terms
                        )
                if not matched_terms or path in expanded_source_paths:
                    continue
                relationship_depth_by_path[path] = min(
                    expansion_depth, relationship_depth_by_path.get(path, expansion_depth)
                )
                expanded_source_paths.add(path)
                frontier.append(path)
                continue
            if path in expanded_source_paths:
                continue
            relationship_depth_by_path[path] = min(
                expansion_depth, relationship_depth_by_path.get(path, expansion_depth)
            )
            relationship_expansion_paths.add(path)
            expanded_source_paths.add(path)
            frontier.append(path)
        frontier.sort(key=expansion_path_priority)

    candidates = []
    all_candidate_paths = sorted(
        seed_candidate_paths | relationship_expansion_paths,
        key=lambda path: (
            0 if path in dependency_manifest_paths else
            1 if path in test_runner_config_paths else
            2 if path in related_container_config_paths else
            3 if path in referenced_module_paths else
            4 if path in codegraph_by_path or path in codegraph_callees_by_path else
            5 if path in direct_anchor_search_paths else
            6 if path in paired_controller_route_paths else
            7 if path in direct_controller_import_paths else
            8 if path in relationship_expansion_paths and set(_neighbor_categories(path)) & _SYSTEM_BOUNDARY_CATEGORIES else
            9 if path in relationship_expansion_paths else
            10 if path in module_name_search_paths else
            11 if path in test_paths else
            12 if path in grep_paths and set(_neighbor_categories(path)) & _SYSTEM_BOUNDARY_CATEGORIES else
            13 if path in grep_paths and "test" in _neighbor_categories(path) else
            14 if path in grep_paths else 15,
            relationship_depth_by_path.get(path, 0),
            -len(_path_tokens(path) & changed_path_tokens),
            path,
        ),
    )
    candidate_scan_limit = min(_DISCOVERY_MAX_CANDIDATE_SCAN_PATHS, max_candidates * 2)
    candidate_paths = all_candidate_paths[:candidate_scan_limit]
    candidate_paths_skipped_by_size = []
    direct_diff_anchor_context_range_candidates = []
    direct_diff_anchor_context_ranges_omitted_by_path = 0
    direct_diff_anchor_context_range_omitted_examples = []
    direct_diff_anchor_match_lines_omitted = 0
    direct_diff_anchor_match_line_omitted_examples = []
    controller_handler_context_ranges_added = 0
    controller_handler_context_omitted = 0
    controller_handler_context_omitted_examples = []
    candidate_match_terms = tuple(
        dict.fromkeys((*ordered_symbols, *changed_diff_terms, *relationship_expansion_terms))
    )
    changed_diff_term_set = set(changed_diff_terms)
    relationship_anchor_terms = {*changed_diff_terms, *relationship_expansion_terms}
    for path in candidate_paths:
        if path not in tracked_set:
            continue
        if path in candidate_source_cache:
            source = candidate_source_cache[path]
        else:
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
        direct_diff_anchor_lines: dict[int, set[str]] = {}
        source_lines = source.splitlines(keepends=True)
        source_text_lines = text.splitlines()
        if candidate_match_terms:
            alternatives = "|".join(re.escape(symbol) for symbol in candidate_match_terms)
            expression = re.compile(
                rf"(?<![A-Za-z0-9_$])(?P<symbol>{alternatives})(?![A-Za-z0-9_$])"
            )
            for line_number, line in enumerate(source_text_lines, 1):
                direct_line_terms = set()
                for match in expression.finditer(line):
                    symbol = match.group("symbol")
                    lines = matches_by_symbol.setdefault(symbol, [])
                    if (not lines or lines[-1] != line_number) and len(lines) < 9:
                        lines.append(line_number)
                    if symbol in changed_diff_term_set:
                        direct_line_terms.add(symbol)
                if direct_line_terms:
                    if line_number in direct_diff_anchor_lines:
                        direct_diff_anchor_lines[line_number].update(direct_line_terms)
                    elif len(direct_diff_anchor_lines) < _DISCOVERY_MAX_DIRECT_DIFF_ANCHOR_MATCH_LINES_PER_PATH:
                        direct_diff_anchor_lines[line_number] = direct_line_terms
                    else:
                        direct_diff_anchor_match_lines_omitted += 1
                        direct_diff_anchor_match_line_omitted_examples.append(path)
        matches = [
            {"symbol": symbol, "lines": lines[:8], "more_lines": len(lines) > 8}
            for symbol, lines in sorted(matches_by_symbol.items())
        ]
        relationship_anchors = sorted(set(matches_by_symbol) & relationship_anchor_terms)
        dispatched_action_anchors = _dispatched_action_anchors(
            source, relationship_anchors
        )
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
        if path in test_runner_config_paths and "test_runner_config" not in categories:
            categories.append("test_runner_config")
        reasons = []
        if matches:
            reasons.append("symbol_reference")
        if relationship_anchors:
            reasons.append("relationship_expansion_anchor")
        if dispatched_action_anchors:
            reasons.append("dispatched_action_reference")
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
        if path in test_runner_config_paths:
            reasons.append("test_runner_configuration")
        if path in related_container_config_paths:
            reasons.append("related_deployment_config")
        if path in paired_controller_route_paths:
            reasons.append("paired_controller_route")
        if path in direct_controller_import_paths:
            reasons.append("direct_controller_import")
        direct_handler_import_references = [
            {
                "controller_path": controller_path,
                "symbols": sorted(references["symbols"]),
                "called_symbols": sorted(references["called_symbols"]),
            }
            for controller_path, references in sorted(
                direct_controller_import_references_by_path.get(path, {}).items()
            )
        ]
        if direct_handler_import_references:
            reasons.append("direct_controller_handler_import_reference")
        direct_handler_import_call = any(
            item["called_symbols"] for item in direct_handler_import_references
        )
        if direct_handler_import_call:
            reasons.append("direct_controller_handler_import_call")
        direct_handler_import_call_matches_changed_anchor = (
            direct_handler_import_call
            and bool(set(relationship_anchors) & changed_diff_term_set)
        )
        if direct_handler_import_call_matches_changed_anchor:
            reasons.append("direct_controller_handler_import_call_matches_changed_anchor")
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
                "relationship_depth": relationship_depth_by_path.get(path, 0),
                "relationship_anchors": relationship_anchors,
                "dispatched_action_anchors": list(dispatched_action_anchors),
                "direct_relationship_anchor": path in direct_anchor_search_paths,
                "codegraph_callers": codegraph_matches,
                "codegraph_callees": codegraph_callee_matches,
                "reasons": reasons,
                "direct_controller_handler_import_references": direct_handler_import_references,
                "direct_controller_handler_import_call_matches_changed_anchor": direct_handler_import_call_matches_changed_anchor,
                "imported_changed_paths": imported_changed_paths,
                "test_audit_candidates": test_audit["candidates"] if test_audit else [],
                "size_bytes": len(source),
                "sha256": hashlib.sha256(source).hexdigest(),
            }
        )
        if (
            path not in changed
            and len(source) >= _DISCOVERY_MIN_SOURCE_BYTES_FOR_DIRECT_DIFF_ANCHOR_RANGE
            and direct_diff_anchor_lines
        ):
            proposed_ranges = []
            for line_number, terms in sorted(direct_diff_anchor_lines.items()):
                start = max(
                    1,
                    line_number - _DISCOVERY_DIRECT_DIFF_ANCHOR_CONTEXT_BEFORE,
                )
                end = min(
                    len(source_lines),
                    line_number + _DISCOVERY_DIRECT_DIFF_ANCHOR_CONTEXT_AFTER,
                )
                controller_handler_context = False
                if (
                    _CONTROLLER_PATH.match(path)
                    and PurePosixPath(path).suffix.lower()
                    in _CONTROLLER_SOURCE_SUFFIXES
                ):
                    handler_start = _controller_handler_start_line(
                        path, source_text_lines, line_number
                    )
                    if handler_start is None:
                        controller_handler_context_omitted += 1
                        controller_handler_context_omitted_examples.append(path)
                    else:
                        handler_range = _controller_handler_range(
                            path, source_text_lines, line_number
                        )
                        if handler_range is None:
                            controller_handler_context_omitted += 1
                            controller_handler_context_omitted_examples.append(path)
                            handler_start = None
                        else:
                            handler_start, handler_end = handler_range
                    if handler_start is not None:
                        handler_range_bytes = b"".join(
                            source_lines[handler_start - 1 : handler_end]
                        )
                        if (
                            handler_end - handler_start + 1
                            <= _DISCOVERY_MAX_CONTROLLER_HANDLER_CONTEXT_LINES
                            and len(handler_range_bytes)
                            <= _DISCOVERY_MAX_CONTROLLER_HANDLER_CONTEXT_BYTES
                        ):
                            controller_handler_context = (
                                handler_start != start or handler_end != end
                            )
                            start = min(start, handler_start)
                            end = handler_end
                            if controller_handler_context:
                                controller_handler_context_ranges_added += 1
                        else:
                            controller_handler_context_omitted += 1
                            controller_handler_context_omitted_examples.append(path)
                if proposed_ranges and start <= proposed_ranges[-1][1] + 3:
                    (
                        previous_start,
                        previous_end,
                        previous_terms,
                        previous_handler_context,
                    ) = proposed_ranges[-1]
                    previous_terms.update(terms)
                    proposed_ranges[-1] = (
                        previous_start,
                        max(previous_end, end),
                        previous_terms,
                        previous_handler_context or controller_handler_context,
                    )
                else:
                    proposed_ranges.append(
                        (start, end, set(terms), controller_handler_context)
                    )
            direct_diff_anchor_context_ranges_omitted_by_path += max(
                0,
                len(proposed_ranges)
                - _DISCOVERY_MAX_DIRECT_DIFF_ANCHOR_CONTEXT_RANGES_PER_PATH,
            )
            direct_diff_anchor_context_range_omitted_examples.extend(
                [path]
                * max(
                    0,
                    len(proposed_ranges)
                    - _DISCOVERY_MAX_DIRECT_DIFF_ANCHOR_CONTEXT_RANGES_PER_PATH,
                )
            )
            for start, end, range_terms, handler_context in proposed_ranges[
                :_DISCOVERY_MAX_DIRECT_DIFF_ANCHOR_CONTEXT_RANGES_PER_PATH
            ]:
                range_bytes = b"".join(source_lines[start - 1 : end])
                if not range_bytes or not range_terms:
                    continue
                range_match_lines = {
                    term: [
                        line_number
                        for line_number, terms in direct_diff_anchor_lines.items()
                        if start <= line_number <= end and term in terms
                    ]
                    for term in range_terms
                }
                direct_diff_anchor_context_range_candidates.append(
                    {
                        "path": path,
                        "categories": list(_neighbor_categories(path)),
                        "matched_symbols": [
                            {
                                "symbol": term,
                                "lines": range_match_lines[term][:8],
                                "more_lines": len(range_match_lines[term]) > 8,
                            }
                            for term in sorted(range_terms)
                        ],
                        "relationship_depth": relationship_depth_by_path.get(path, 0),
                        "relationship_anchors": sorted(range_terms),
                        "dispatched_action_anchors": [],
                        "direct_relationship_anchor": path in direct_anchor_search_paths,
                        "direct_diff_anchor_context_range": True,
                        "direct_controller_handler_context_range": handler_context,
                        "codegraph_callers": [],
                        "codegraph_callees": [],
                        "reasons": [
                            "direct_changed_diff_anchor_range",
                            *(
                                ["direct_controller_handler_context"]
                                if handler_context
                                else []
                            ),
                        ],
                        "imported_changed_paths": [],
                        "test_audit_candidates": [],
                        "size_bytes": len(range_bytes),
                        "sha256": hashlib.sha256(range_bytes).hexdigest(),
                        "start_line": start,
                        "end_line": end,
                    }
                )

    changed_file_context_ranges_added = 0
    changed_file_context_ranges_omitted = 0
    changed_file_context_range_omitted_examples = []
    diff_context_ranges_by_path: dict[str, list[tuple[int, int]]] = {}
    for hunk in diff_hunk_inventory.get("hunks", []):
        path_index = hunk.get("changed_path_index")
        if not isinstance(path_index, int) or path_index >= len(context.changed_files):
            continue
        path = context.changed_files[path_index]
        start = max(1, int(hunk["new_start"]) - 8)
        end = int(hunk["new_start"]) + max(1, int(hunk["new_count"])) - 1 + 8
        diff_context_ranges_by_path.setdefault(path, []).append((start, end))

    for path, terms in sorted(changed_file_relationship_terms_by_path.items()):
        source = changed_sources.get(path)
        if source is None or not terms:
            continue
        source_lines = _source_lines(source)
        try:
            decoded_lines = [line.decode("utf-8") for line in source_lines]
        except UnicodeDecodeError:
            continue
        route_terms = changed_file_route_terms_by_path.get(path, set())
        matching_lines: dict[int, set[str]] = {}
        route_lines = set()
        for line_number, line in enumerate(decoded_lines, start=1):
            found = _matching_relationship_terms(line.encode("utf-8"), tuple(terms))
            if not found:
                continue
            matching_lines[line_number] = found
            if any(
                re.search(rf"/{re.escape(term)}(?:/|[?#\"'`]|\b)", line, re.IGNORECASE)
                for term in route_terms
            ):
                route_lines.add(line_number)
        selected_lines = sorted(route_lines or set(matching_lines))
        exclusion_ranges = diff_context_ranges_by_path.get(path, [])
        selected_lines = [
            line_number
            for line_number in selected_lines
            if not any(start <= line_number <= end for start, end in exclusion_ranges)
        ]
        if not selected_lines:
            continue

        proposed_ranges = []
        for line_number in selected_lines:
            is_route_line = line_number in route_lines
            before, after = (16, 40) if is_route_line else (3, 3)
            start = max(1, line_number - before)
            end = min(len(source_lines), line_number + after)
            if is_route_line and any(
                start <= hunk_end and end >= hunk_start
                for hunk_start, hunk_end in exclusion_ranges
            ):
                start = max(1, line_number - 3)
                end = min(len(source_lines), line_number + 3)
            proposed_ranges.append((start, end))
        merged_ranges = []
        for start, end in sorted(proposed_ranges):
            if merged_ranges and start <= merged_ranges[-1][1] + 8:
                merged_ranges[-1] = (merged_ranges[-1][0], max(merged_ranges[-1][1], end))
            else:
                merged_ranges.append((start, end))
        range_limit = min(
            _DISCOVERY_MAX_CHANGED_FILE_CONTEXT_RANGES_PER_PATH,
            max(0, _DISCOVERY_MAX_CHANGED_FILE_CONTEXT_RANGES - changed_file_context_ranges_added),
        )
        changed_file_context_ranges_omitted += max(0, len(merged_ranges) - range_limit)
        if len(merged_ranges) > range_limit:
            changed_file_context_range_omitted_examples.extend(
                [path] * (len(merged_ranges) - range_limit)
            )
        for start, end in merged_ranges[:range_limit]:
            if any(
                start <= hunk_end and end >= hunk_start
                for hunk_start, hunk_end in exclusion_ranges
            ):
                continue
            range_bytes = b"".join(source_lines[start - 1 : end])
            range_terms = sorted(
                {
                    term
                    for line_number in range(start, end + 1)
                    for term in matching_lines.get(line_number, set())
                }
            )
            range_match_lines: dict[str, list[int]] = {}
            for line_number in range(start, end + 1):
                for term in matching_lines.get(line_number, set()):
                    line_numbers = range_match_lines.setdefault(term, [])
                    if len(line_numbers) < 8:
                        line_numbers.append(line_number)
            candidates.append(
                {
                    "path": path,
                    "categories": list(_neighbor_categories(path)),
                    "matched_symbols": [
                        {"symbol": term, "lines": range_match_lines[term], "more_lines": False}
                        for term in range_terms
                    ],
                    "relationship_depth": relationship_depth_by_path.get(path, 0),
                    "relationship_anchors": range_terms,
                    "direct_relationship_anchor": path in direct_anchor_search_paths,
                    "changed_file_context_range": True,
                    "start_line": start,
                    "end_line": end,
                    "codegraph_callers": [],
                    "codegraph_callees": [],
                    "reasons": ["changed_file_relationship_range", "relationship_expansion_anchor"],
                    "imported_changed_paths": [],
                    "test_audit_candidates": [],
                    "size_bytes": len(range_bytes),
                    "sha256": hashlib.sha256(range_bytes).hexdigest(),
                }
            )
            changed_file_context_ranges_added += 1

    def direct_context_range_priority(item: dict) -> tuple:
        categories = set(item["categories"])
        return (
            0 if item.get("direct_controller_handler_context_range") else
            1 if categories & _SYSTEM_BOUNDARY_CATEGORIES else
            2 if item["direct_relationship_anchor"] else 3,
            -len(item["relationship_anchors"]),
            item["size_bytes"],
            item["path"],
            item["start_line"],
        )

    direct_diff_anchor_context_range_candidates.sort(key=direct_context_range_priority)
    direct_diff_anchor_context_range_limit = min(
        _DISCOVERY_MAX_DIRECT_DIFF_ANCHOR_CONTEXT_RANGES,
        max_candidates,
    )
    direct_diff_anchor_context_ranges_omitted = (
        direct_diff_anchor_context_ranges_omitted_by_path
        + max(0, len(direct_diff_anchor_context_range_candidates) - direct_diff_anchor_context_range_limit)
    )
    direct_diff_anchor_context_range_omitted_examples.extend(
        item["path"]
        for item in direct_diff_anchor_context_range_candidates[
            direct_diff_anchor_context_range_limit:
        ]
    )
    direct_diff_anchor_context_ranges = direct_diff_anchor_context_range_candidates[
        :direct_diff_anchor_context_range_limit
    ]
    candidates.extend(direct_diff_anchor_context_ranges)

    def candidate_priority(item: dict) -> tuple:
        categories = set(item["categories"])
        is_code_source = _relationship_expandable_source(item["path"])
        is_local_import = item["path"] in referenced_module_paths
        has_action_anchor = bool(set(item["relationship_anchors"]) & _ACTION_ARGUMENT_TERMS)
        has_dispatched_action = bool(item.get("dispatched_action_anchors"))
        priority = (
            0 if "dependency" in categories else
            1 if "test_runner_config" in categories else
            2 if is_code_source and item.get("changed_file_context_range") else
            3 if "related_deployment_config" in item["reasons"] else
            4 if is_code_source and has_dispatched_action else
            5 if is_code_source and is_local_import and item["direct_relationship_anchor"] else
            6 if is_code_source and has_action_anchor else
            7 if is_code_source and is_local_import else
            8 if is_code_source and _COMMAND_ENTRYPOINT_PATH.search(item["path"]) else
            9 if is_code_source and "direct_controller_import" in item["reasons"] and _VALIDATOR_PATH.search(item["path"]) else
            10 if is_code_source and item.get("direct_controller_handler_context_range") else
            11 if is_code_source and item.get("direct_diff_anchor_context_range") else
            12 if is_code_source and "paired_controller_route" in item["reasons"] else
            13 if is_code_source and "direct_controller_import" in item["reasons"] and "/middleware/" in item["path"].lower() else
            14 if is_code_source and item.get("direct_controller_handler_import_call_matches_changed_anchor") else
            15 if is_code_source and "direct_controller_handler_import_call" in item["reasons"] else
            16 if is_code_source and "direct_controller_handler_import_reference" in item["reasons"] else
            17 if is_code_source and "direct_controller_import" in item["reasons"] else
            18 if is_code_source and item["direct_relationship_anchor"] else
            19 if is_code_source and categories & _SYSTEM_BOUNDARY_CATEGORIES else
            20 if is_code_source and item["relationship_depth"] > 0 else
            21 if is_code_source and "declaration" in categories else
            22 if "test" in categories else
            23 if is_code_source else
            24 if is_local_import else
            25 if item["direct_relationship_anchor"] else
            26 if item["relationship_depth"] > 0 else
            27 if "declaration" in categories else 28
        )
        return (
            priority,
            -len(item["relationship_anchors"]),
            item["relationship_depth"],
            -len(_path_tokens(item["path"]) & changed_path_tokens),
            item["path"],
        )

    candidates.sort(key=candidate_priority)
    candidate_results_considered = len(candidates)
    candidate_results_omitted_by_limit = max(0, candidate_results_considered - max_candidates)
    candidate_result_omission_examples = [
        item["path"] for item in candidates[max_candidates:]
    ]
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
            start_line=candidate.get("start_line"),
            end_line=candidate.get("end_line"),
            source_text_available=True,
        )
        used_refs.add(record.evidence_ref)
        next_id += 1
        if not candidate.get("direct_diff_anchor_context_range"):
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
        "candidate_result_limit": "Eligible evidence candidates exceeded the maximum records included in the discovery packet.",
        "test_runner_config_limit": "Only the highest-ranked bounded runner configs and CI workflows are suggested for changed tests.",
        "test_workflow_scan_limit": "Only a bounded number and byte size of likely CI workflow files are inspected for test commands.",
        "candidate_source_size_limit": "A candidate source exceeded a per-file or total-source byte cap.",
        "module_search_term_limit": "Only the configured number of module-name and symbol terms are searched.",
        "added_diff_anchor_term_limit": "Only a bounded number of distinct added-side identifiers within the per-term byte limit are searched.",
        "removed_diff_anchor_term_limit": "Only a bounded number of distinct removed-side identifiers within the per-term byte limit are searched.",
        "relationship_expansion_frontier_limit": "Only the highest-ranked candidate sources are expanded at each bounded depth.",
        "relationship_expansion_term_limit": "Only the configured number of route and symbol anchors are searched per expansion round.",
        "relationship_expansion_source_size_limit": "Candidate sources exceeded a per-file or total-source byte cap before relationship expansion.",
        "changed_file_context_range_limit": "Only a bounded number of additional source ranges are attached from changed files.",
        "direct_diff_anchor_context_range_limit": "Only a bounded number of compact neighbor source ranges are attached around exact added-diff anchors.",
        "direct_diff_anchor_match_line_limit": "Only a bounded number of exact added-diff anchor matches are retained per neighbor source file.",
        "controller_handler_context_limit": "A controller handler declaration was not included because no bounded JavaScript or TypeScript handler boundary was found within the line and byte limits.",
        "direct_controller_context_limit": "Only a bounded number of directly matched controller files are paired with route registries and import dependencies.",
        "controller_route_pair_limit": "Only a bounded number of conventional route files paired with directly matched controllers are included.",
        "direct_controller_import_limit": "Only a bounded number of static imports from directly matched controllers and their paired routes are included.",
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
        "candidate_result_limit",
        candidate_results_omitted_by_limit,
        candidate_result_omission_examples,
        max_candidates,
    )
    add_omission(
        "test_runner_config_limit",
        len(test_runner_config_paths_omitted),
        test_runner_config_paths_omitted,
        _DISCOVERY_MAX_TEST_RUNNER_CONFIG_CANDIDATES,
    )
    add_omission(
        "test_workflow_scan_limit",
        len(test_workflow_scan_omitted),
        test_workflow_scan_omitted,
        _DISCOVERY_MAX_TEST_WORKFLOW_SCAN_PATHS,
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
        "added_diff_anchor_term_limit",
        added_diff_terms_omitted,
        [],
        _DISCOVERY_MAX_SYMBOLS,
    )
    add_omission(
        "removed_diff_anchor_term_limit",
        removed_diff_terms_omitted,
        [],
        _DISCOVERY_MAX_SYMBOLS,
    )
    add_omission(
        "relationship_expansion_frontier_limit",
        relationship_expansion_frontier_omitted,
        relationship_expansion_frontier_omitted_examples,
        expansion_path_limit,
    )
    add_omission(
        "relationship_expansion_term_limit",
        relationship_expansion_terms_omitted,
        relationship_expansion_term_omitted_examples,
        _DISCOVERY_MAX_RELATIONSHIP_EXPANSION_TERMS,
    )
    add_omission(
        "relationship_expansion_source_size_limit",
        len(relationship_expansion_source_paths_skipped_by_size),
        relationship_expansion_source_paths_skipped_by_size,
        _DISCOVERY_MAX_SOURCE_BYTES,
    )
    add_omission(
        "changed_file_context_range_limit",
        changed_file_context_ranges_omitted,
        changed_file_context_range_omitted_examples,
        _DISCOVERY_MAX_CHANGED_FILE_CONTEXT_RANGES,
    )
    add_omission(
        "direct_diff_anchor_context_range_limit",
        direct_diff_anchor_context_ranges_omitted,
        direct_diff_anchor_context_range_omitted_examples,
        _DISCOVERY_MAX_DIRECT_DIFF_ANCHOR_CONTEXT_RANGES,
    )
    add_omission(
        "direct_diff_anchor_match_line_limit",
        direct_diff_anchor_match_lines_omitted,
        direct_diff_anchor_match_line_omitted_examples,
        _DISCOVERY_MAX_DIRECT_DIFF_ANCHOR_MATCH_LINES_PER_PATH,
    )
    add_omission(
        "controller_handler_context_limit",
        controller_handler_context_omitted,
        controller_handler_context_omitted_examples,
        _DISCOVERY_MAX_CONTROLLER_HANDLER_CONTEXT_LINES,
    )
    add_omission(
        "controller_route_pair_limit",
        paired_controller_route_paths_omitted,
        sorted(paired_controller_route_candidates)[
            _DISCOVERY_MAX_CONTROLLER_ROUTE_PAIRS:
        ],
        _DISCOVERY_MAX_CONTROLLER_ROUTE_PAIRS,
    )
    add_omission(
        "direct_controller_context_limit",
        direct_controller_paths_omitted,
        list(direct_controller_candidates[_DISCOVERY_MAX_CONTROLLER_ROUTE_PAIRS:]),
        _DISCOVERY_MAX_CONTROLLER_ROUTE_PAIRS,
    )
    add_omission(
        "direct_controller_import_limit",
        direct_controller_import_paths_omitted,
        sorted(direct_controller_import_candidates - set(direct_controller_import_paths)),
        _DISCOVERY_MAX_DIRECT_CONTROLLER_IMPORTS,
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
            "languages": ["Python", "Ruby", "JavaScript", "TypeScript"],
            "method": (
                "bounded pinned-source import parsing; Ruby supports static literal require "
                "and require_relative only"
            ),
            "exhaustive": False,
        },
        {
            "kind": "tests_and_test_dependencies",
            "method": "path conventions, selected test audits, ancestor manifests, and bounded runner configs",
            "exhaustive": False,
        },
        {
            "kind": "test_runner_configuration",
            "candidate_paths": test_runner_config_paths,
            "candidate_paths_omitted_by_limit": len(test_runner_config_paths_omitted),
            "workflow_paths_scanned": test_workflow_paths_scanned,
            "workflow_paths_omitted_by_scan_limit": len(test_workflow_scan_omitted),
            "workflow_scan_bytes_limit": _DISCOVERY_MAX_TEST_WORKFLOW_SCAN_BYTES,
            "exhaustive": False,
        },
        {
            "kind": "bounded_candidate_relationship_expansion",
            "depth": _DISCOVERY_MAX_RELATIONSHIP_EXPANSION_ROUNDS,
            "candidate_sources_read": relationship_expansion_sources_read,
            "terms_searched": len(relationship_expansion_terms),
            "paths_added": len(relationship_expansion_paths),
            "exhaustive": False,
        },
        {
            "kind": "matched_ranges_in_changed_files",
            "range_count": changed_file_context_ranges_added,
            "exhaustive": False,
        },
        {
            "kind": "neighbor_ranges_around_added_diff_anchors",
            "range_count": sum(
                bool(item.get("direct_diff_anchor_context_range")) for item in records
            ),
            "exhaustive": False,
        },
        {
            "kind": "direct_controller_handler_context",
            "range_count": sum(
                bool(item.get("direct_controller_handler_context_range"))
                for item in records
            ),
            "omitted_anchor_count": controller_handler_context_omitted,
            "max_lines": _DISCOVERY_MAX_CONTROLLER_HANDLER_CONTEXT_LINES,
            "max_bytes": _DISCOVERY_MAX_CONTROLLER_HANDLER_CONTEXT_BYTES,
            "exhaustive": False,
        },
        {
            "kind": "conventional_controller_route_pairs",
            "path_count": len(paired_controller_route_paths),
            "exhaustive": False,
        },
        {
            "kind": "direct_controller_imports",
            "path_count": len(direct_controller_import_paths),
            "exhaustive": False,
        },
        {
            "kind": "system_boundary_path_candidates",
            "categories": sorted(_SYSTEM_BOUNDARY_CATEGORIES),
            "method": (
                "path classification, bounded symbol/reference matches, and paired "
                "Dockerfile/Compose configuration candidates"
            ),
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
        {
            "kind": "unsupported_language_imports",
            "reason": (
                "Local import resolution covers Python, JavaScript/TypeScript, and static Ruby "
                "require forms; imports in other languages are not systematically traced."
            ),
        },
        {
            "kind": "ruby_dynamic_loads_and_autoload",
            "reason": (
                "Dynamic Ruby require paths, $LOAD_PATH semantics, Rails autoloading, and "
                "runtime-generated links are not resolved as static import edges."
            ),
        },
        {"kind": "api_and_serialized_contract_registration", "reason": "Bounded route and identifier expansion supplements direct imports, but framework route registries and serialized contracts are not resolved semantically."},
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
        "added_diff_anchor_terms": list(added_diff_terms),
        "added_diff_anchor_terms_omitted": added_diff_terms_omitted,
        "removed_diff_anchor_terms": list(removed_diff_terms),
        "removed_diff_anchor_terms_omitted": removed_diff_terms_omitted,
        "diff_anchor_term_max_bytes": _DISCOVERY_MAX_DIFF_ANCHOR_TERM_BYTES,
        "changed_diff_anchor_terms": list(changed_diff_terms),
        "changed_anchor_search_paths": len(direct_anchor_search_paths),
        "changed_anchor_search_bytes": direct_anchor_search_bytes,
        "module_search_terms": list(module_search_terms),
        "module_search_terms_omitted": max(
            0, len(all_module_search_terms) - len(module_search_terms)
        ),
        "module_name_search_paths": len(module_name_search_paths),
        "module_name_search_bytes": module_name_search_bytes,
        "module_name_search_bytes_limit": _DISCOVERY_MAX_MODULE_SEARCH_BYTES,
        "module_name_search_exhaustive": False,
        "relationship_expansion": {
            "max_depth": _DISCOVERY_MAX_RELATIONSHIP_EXPANSION_ROUNDS,
            "max_candidate_sources_per_round": expansion_path_limit,
            "candidate_sources_considered": relationship_expansion_sources_considered,
            "candidate_sources_read": relationship_expansion_sources_read,
            "candidate_sources_omitted_by_limit": relationship_expansion_frontier_omitted,
            "candidate_sources_omitted_by_size": len(relationship_expansion_source_paths_skipped_by_size),
            "direct_diff_anchor_context_ranges_added": sum(
                bool(item.get("direct_diff_anchor_context_range")) for item in records
            ),
            "direct_controller_handler_context_ranges_added": sum(
                bool(item.get("direct_controller_handler_context_range"))
                for item in records
            ),
            "max_terms_per_round": _DISCOVERY_MAX_RELATIONSHIP_EXPANSION_TERMS,
            "terms_searched": relationship_expansion_terms,
            "terms_omitted_by_limit": relationship_expansion_terms_omitted,
            "paths_added": len(relationship_expansion_paths),
            "changed_file_ranges_added": changed_file_context_ranges_added,
            "search_bytes": relationship_expansion_search_bytes,
            "search_bytes_limit": _DISCOVERY_MAX_MODULE_SEARCH_BYTES,
            "exhaustive": False,
        },
        "changed_source_symbol_collection_truncated": not collect_changed_symbols,
        "changed_sources_skipped_symbol_scan": changed_sources_skipped_symbol_scan,
        "changed_sources_skipped_symbol_scan_examples": changed_sources_skipped_symbol_scan_paths[:_DISCOVERY_MAX_OMISSION_EXAMPLES],
        "candidate_records": records,
        "direct_diff_anchor_context_ranges_added": sum(
            bool(item.get("direct_diff_anchor_context_range")) for item in records
        ),
        "direct_diff_anchor_context_ranges_omitted": direct_diff_anchor_context_ranges_omitted,
        "candidate_manifest": candidate_manifest,
        "candidate_count": len(candidate_manifest),
        "candidate_results_considered": candidate_results_considered,
        "candidate_results_omitted_by_limit": candidate_results_omitted_by_limit,
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
    batch = subparsers.add_parser("prepare-batch", help="Prepare each review input independently")
    batch.add_argument("--manifest", required=True, type=Path)
    check_batch = subparsers.add_parser("check-batch", help="Check per-input host outcome records")
    check_batch.add_argument("--manifest", required=True, type=Path)
    check_batch.add_argument("--results", required=True, type=Path)
    trace = subparsers.add_parser("check-agent-trace", help="Evaluate a normalized host trace")
    trace.add_argument("--manifest", required=True, type=Path)
    trace.add_argument("--trace", required=True, type=Path)
    resolve = subparsers.add_parser("resolve-git", help="Pin two local Git refs and their diff")
    resolve.add_argument("--repository", required=True, type=Path)
    resolve.add_argument("--base", required=True)
    resolve.add_argument("--head", required=True)
    resolve.add_argument("--output", type=Path)
    bind_diff = subparsers.add_parser(
        "bind-diff", help="Bind a local unified diff without a repository"
    )
    bind_diff.add_argument("--context", required=True, type=Path)
    bind_diff.add_argument("--diff", required=True, type=Path)
    bind_diff.add_argument("--output", required=True, type=Path)
    pack_diff = subparsers.add_parser(
        "pack-diff", help="Build a diff-only packet without a repository"
    )
    pack_diff.add_argument("--context", required=True, type=Path)
    pack_diff.add_argument("--diff", required=True, type=Path)
    pack_diff.add_argument("--content", required=True, type=Path)
    pack_diff.add_argument("--packet", required=True, type=Path)
    pack_diff.add_argument("--bound-context", required=True, type=Path)
    build = subparsers.add_parser("build", help="Build a registry from host-provided ref metadata")
    verify = subparsers.add_parser(
        "verify", help="Check evidence against immutable local Git objects"
    )
    check_links = subparsers.add_parser(
        "check-links",
        help="Check allowlisted external evidence URLs on demand",
    )
    check_links.add_argument("--context", required=True, type=Path)
    check_links.add_argument("--allow-host", action="append", required=True, metavar="HOST")
    check_links.add_argument("--timeout", type=float, default=10.0)
    check_links.add_argument("--output", type=Path)
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
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include callers and callees from a current CodeGraph index when available (default: enabled)",
    )
    discover.add_argument(
        "--bound-context",
        type=Path,
        help="Write a review context bound to this discovery result and exact candidate set",
    )
    preview = subparsers.add_parser(
        "preview-selection", help="Preview a profile's selected packet without running a model"
    )
    preview.add_argument("--context", required=True, type=Path)
    preview.add_argument("--packet", required=True, type=Path)
    preview.add_argument("--profile", required=True)
    preview.add_argument("--review-profile", action="append", default=[])
    preview.add_argument("--max-bytes", type=int, default=1_000_000)
    preview.add_argument("--output", type=Path)
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
    if args.command in {"prepare-batch", "check-batch", "check-agent-trace"}:
        from qa_orchestrator.review_batch import (
            check_agent_trace,
            check_review_batch,
            prepare_review_batch,
        )

        if args.command == "prepare-batch":
            result = prepare_review_batch(args.manifest)
            ok = all(item["status"] != "failed" for item in result["results"])
        elif args.command == "check-batch":
            result = check_review_batch(args.manifest, args.results)
            ok = not result["unresolved"]
        else:
            result = check_agent_trace(args.manifest, args.trace)
            ok = not result["unresolved"] and result["final_seen"]
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if ok else 1
    if args.command == "pack-diff":
        from qa_orchestrator.context_pack import build_diff_context_packet

        inputs = {args.context.resolve(), args.diff.resolve(), args.content.resolve()}
        outputs = {args.packet.resolve(), args.bound_context.resolve()}
        if len(outputs) != 2 or inputs & outputs:
            raise ValueError("packet and bound context outputs must be distinct from all inputs")
        context = ReviewContext.model_validate_json(args.context.read_bytes())
        diff = args.diff.read_bytes()
        bound = bind_diff_only(context, diff)
        content = json.loads(args.content.read_text(encoding="utf-8"))
        if not isinstance(content, dict) or any(not isinstance(v, str) for v in content.values()):
            raise ValueError("content must map evidence refs to text")
        packet = build_diff_context_packet(bound, diff, external_content=content)
        packet_bytes = (packet.model_dump_json() + "\n").encode("utf-8")
        bound = ReviewContext.model_validate(
            {
                **bound.model_dump(),
                "evidence_packet_sha256": hashlib.sha256(packet_bytes).hexdigest(),
            }
        )
        args.packet.write_bytes(packet_bytes)
        args.bound_context.write_text(bound.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return 0
    if args.command == "bind-diff":
        if args.output.resolve() in {args.context.resolve(), args.diff.resolve()}:
            raise ValueError("bound context output must be distinct from inputs")
        context = ReviewContext.model_validate_json(args.context.read_bytes())
        bound = bind_diff_only(context, args.diff.read_bytes())
        args.output.write_text(bound.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return 0
    if args.command == "resolve-git":
        result = resolve_git_input(args.repository, args.base, args.head)
        output = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.write_text(output, encoding="utf-8")
        else:
            print(output, end="")
        return 0
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
    if args.command == "check-links":
        from qa_orchestrator.link_audit import check_evidence_links, link_audit_failed

        if args.output and args.output.resolve() == args.context.resolve():
            raise ValueError("link audit output must use a distinct path from the context")
        context = ReviewContext.model_validate_json(args.context.read_bytes())
        result = check_evidence_links(
            context.evidence_registry,
            allowed_hosts=tuple(args.allow_host),
            timeout_seconds=args.timeout,
        )
        output = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.write_text(output, encoding="utf-8")
        else:
            print(output, end="")
        return 1 if link_audit_failed(result) else 0
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
    if args.command == "preview-selection":
        from qa_orchestrator.codex_executor import _profile_context_override_refs
        from qa_orchestrator.context_pack import (
            ContextPacket,
            select_context,
            validate_packet_context,
        )
        from qa_orchestrator.contracts import ReviewAgent

        if args.output and args.output.resolve() in {args.context.resolve(), args.packet.resolve()}:
            raise ValueError("selection preview output must differ from context and packet")
        context = ReviewContext.model_validate_json(args.context.read_bytes())
        packet_bytes = args.packet.read_bytes()
        if hashlib.sha256(packet_bytes).hexdigest() != context.evidence_packet_sha256:
            raise ValueError("Evidence packet hash mismatch")
        packet = ContextPacket.model_validate_json(packet_bytes)
        validate_packet_context(packet, context)
        profile = ReviewAgent(args.profile)
        profiles = tuple(ReviewAgent(value) for value in args.review_profile) or (profile,)
        if profile not in profiles:
            raise ValueError("selected profile must be included in review profiles")
        override_refs = _profile_context_override_refs(
            packet, profile, set(context.changed_files), profiles
        )
        selected = select_context(packet, profile, max_bytes=args.max_bytes,
                                  profile_override_refs=override_refs)
        payload = {
            "profile": profile.value, "review_profiles": [p.value for p in profiles],
            "included_refs": selected.included_refs, "omitted_refs": selected.omitted_refs,
            "budget_omitted_refs": selected.budget_omitted_refs,
            "selected_bytes": len(selected.text.encode("utf-8")),
            "selected_sha256": selected.sha256,
            "max_bytes": args.max_bytes,
            "note": "Packet bytes only; full executor prompt overhead may reduce the available budget.",
        }
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
