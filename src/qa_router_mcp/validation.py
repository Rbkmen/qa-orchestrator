import ast
import json
import re

from qa_router_mcp.contracts import DraftEnvelope, DraftKind

FIELD_PATTERNS = {
    "title": re.compile(r"(?im)^\s*(?:[-*#]+\s*)?(?:title|заголовок|название)\s*[:—-]"),
    "preconditions": re.compile(r"(?im)^\s*(?:[-*#]+\s*)?(?:preconditions?|предусловия)\s*[:—-]"),
    "steps": re.compile(r"(?im)^\s*(?:[-*#]+\s*)?(?:steps?|шаги)\s*[:—-]"),
    "expected_result": re.compile(
        r"(?im)^\s*(?:[-*#]+\s*)?(?:expected result|ожидаемый результат)\s*[:—-]"
    ),
}
CASE_HEADING = re.compile(
    r"(?im)^\s*(?:[-*#]+\s*)?(?:test case(?:\s+\d+)?|"
    r"тест[- ]?кейс(?:\s+\d+)?)\s*\**\s*(?:[:—-]|$)"
)
REVIEW_SECTION_PATTERNS = {
    "scope": re.compile(r"(?im)^\s*(?:#{1,6}\s*)?scope(?:\s*[:—-]|\s*$)"),
    "checklist": re.compile(r"(?im)^\s*(?:#{1,6}\s*)?checklist(?:\s*[:—-]|\s*$)"),
    "candidate_coverage_gaps": re.compile(
        r"(?im)^\s*(?:#{1,6}\s*)?candidate coverage gaps(?:\s*[:—-]|\s*$)"
    ),
    "positive_observations": re.compile(
        r"(?im)^\s*(?:#{1,6}\s*)?positive observations(?:\s*[:—-]|\s*$)"
    ),
    "unverified": re.compile(r"(?im)^\s*(?:#{1,6}\s*)?unverified(?:\s*[:—-]|\s*$)"),
}
REVIEW_UNSUPPORTED_DECISION = re.compile(
    r"(?im)^\s*(?:[-*#]+\s*)?(?:severity|priority|root cause|release readiness|"
    r"merge readiness|verdict|confirmed(?: finding)?|confirmation)\s*[:=—-]"
)
COVERAGE_ID = re.compile(r"(?im)^\s*coverage id\s*:\s*([A-Za-z0-9][A-Za-z0-9._-]{0,63})\s*$")
COUNT_WORDS = {
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "два": 2,
    "две": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
    "одиннадцать": 11,
    "двенадцать": 12,
    "тринадцать": 13,
    "четырнадцать": 14,
    "пятнадцать": 15,
    "шестнадцать": 16,
    "семнадцать": 17,
    "восемнадцать": 18,
    "девятнадцать": 19,
    "двадцать": 20,
}
EXTERNAL_WRITE = re.compile(
    r"(?ix)(?:"
    r"\bgit\s+(?:commit|push|merge|tag|reset\s+--hard)\b|"
    r"\bglab\s+(?:mr|issue|release)\s+(?:create|merge|delete|close|update|approve)\b|"
    r"\bkubectl\s+(?:apply|create|delete|patch|replace|scale|edit|set)\b|"
    r"\bcurl\b[^\n]*(?:-X|--request)\s*(?:POST|PUT|PATCH|DELETE)\b|"
    r"\bfetch\s*\([^\n]*(?:method\s*:\s*['\"](?:POST|PUT|PATCH|DELETE)['\"])[^\n]*\)|"
    r"\b(?:requests|httpx|axios)\.(?:post|put|patch|delete)\s*\(|"
    r"\b(?:requests|httpx|axios|client|session)\.request\s*\([^\n]*(?:method\s*=\s*['\"]"
    r"(?:POST|PUT|PATCH|DELETE)['\"]|['\"](?:POST|PUT|PATCH|DELETE)['\"])|"
    r"\b(?:os|shutil)\.(?:remove|unlink|rmdir|rename|replace|makedirs|mkdir|system|"
    r"popen|exec[lv][a-z]*|spawn[lv][a-z]*|rmtree|copy|copy2|copytree|move)\s*\(|"
    r"\bPath\s*\([^\n]*\)\.(?:unlink|write_text|write_bytes|rename|replace|mkdir|touch)\s*\(|"
    r"\bPath\s*\([^\n]*\)\.open\s*\([^\n]*(?:['\"][wax][+b]?['\"]|"
    r"mode\s*=\s*['\"][wax][+b]?['\"])|"
    r"\b(?:rm|unlink|rmdir)\s+(?:-[^\s]+\s+)*[^\s]+|"
    r"\b(?:echo|printf|cat|sed|awk)\b[^\n]*(?:>>?|2>)\s*\S|"
    r"\btee(?:\s+-a)?\s+\S|"
    r"\b(?:writeFile|write_text|write_bytes|appendFile|truncate)\s*\(|"
    r"\bopen\s*\([^\n]*,\s*['\"](?:w|a|x)[+b]?['\"]|"
    r"\bcommit\s*=\s*true\b|"
    r"\bjira_(?:add_comment|transition|create|update|delete)[a-z_]*\b|"
    r"\btestrail_(?:create|update|add|delete)[a-z_]*\b"
    r")"
)


def validate_generated_draft(
    kind: DraftKind,
    request_text: str,
    result: DraftEnvelope,
    *,
    expected_coverage_ids: tuple[str, ...] | None = None,
    preserve_terms: tuple[str, ...] = (),
) -> list[str]:
    if result.status != "ok":
        return []
    if result.generation_stats.truncated:
        return ["truncated"]

    issues: list[str] = []
    if kind == DraftKind.TEST_CASES:
        expected_ids = (
            list(expected_coverage_ids)
            if expected_coverage_ids is not None
            else COVERAGE_ID.findall(request_text)
        )
        if expected_ids:
            actual_ids = COVERAGE_ID.findall(result.draft)
            if len(actual_ids) != len(set(actual_ids)):
                issues.append("test_cases_duplicate_coverage_id")
            if set(actual_ids) - set(expected_ids):
                issues.append("test_cases_unexpected_coverage_id")
            if set(expected_ids) - set(actual_ids):
                issues.append("test_cases_missing_coverage_id")
        expected = requested_case_count(request_text)
        title_fields = list(FIELD_PATTERNS["title"].finditer(result.draft))
        case_headings = list(CASE_HEADING.finditer(result.draft))
        title_matches = title_fields if len(title_fields) >= len(case_headings) else case_headings
        if len(title_matches) < expected:
            issues.append("test_cases_missing_title")
        elif len(title_matches) > expected:
            issues.append("test_cases_wrong_count")
        if title_matches:
            blocks = [
                result.draft[match.start() : next_start]
                for match, next_start in zip(
                    title_matches,
                    [item.start() for item in title_matches[1:]] + [len(result.draft)],
                    strict=True,
                )
            ]
            for field, pattern in FIELD_PATTERNS.items():
                if field != "title" and any(not pattern.search(block) for block in blocks):
                    issues.append(f"test_cases_missing_{field}")
                if any(_has_empty_field(block, pattern) for block in blocks):
                    issues.append(f"test_cases_empty_{field}")
        else:
            issues.extend(
                f"test_cases_missing_{field}" for field in FIELD_PATTERNS if field != "title"
            )
    elif kind == DraftKind.TRANSLATION:
        if any(term not in result.draft for term in preserve_terms if term):
            issues.append("translation_missing_preserve_term")
    elif kind == DraftKind.SHORT_EXPLANATION and len(result.draft.split()) > 120:
        issues.append("short_explanation_too_long")
    elif kind == DraftKind.AUTOMATION_SKELETON and (
        EXTERNAL_WRITE.search(result.draft) or _contains_python_external_write(result.draft)
    ):
        issues.append("automation_external_write")
    elif kind == DraftKind.REVIEW_CHECKLIST:
        issues.extend(
            f"review_missing_{section}"
            for section, pattern in REVIEW_SECTION_PATTERNS.items()
            if not pattern.search(result.draft)
        )
        if EXTERNAL_WRITE.search(result.draft) or _contains_python_external_write(result.draft):
            issues.append("review_external_write")
        if REVIEW_UNSUPPORTED_DECISION.search(result.draft):
            issues.append("review_unsupported_decision")

    return issues


def normalize_test_case_draft(draft: str) -> str:
    """Convert a nested JSON test-case draft to the required plain-text heading format."""
    try:
        parsed = json.loads(draft)
    except (json.JSONDecodeError, TypeError):
        return draft
    items = [parsed] if isinstance(parsed, dict) else parsed
    if (
        not isinstance(items, list)
        or not items
        or not all(isinstance(item, dict) for item in items)
    ):
        return draft

    required = ("coverage id", "title", "preconditions", "steps", "expected result")
    blocks: list[str] = []
    for item in items:
        normalized = {
            str(key).strip().casefold().replace("_", " "): value for key, value in item.items()
        }
        if set(normalized) != set(required) or len(normalized) != len(item):
            return draft
        if any(
            not isinstance(value, str)
            and not (isinstance(value, list) and all(isinstance(part, str) for part in value))
            for value in normalized.values()
        ):
            return draft
        values = {
            key: _plain_text_value(normalized[key], numbered=key == "steps") for key in required
        }
        blocks.append(
            "\n".join(
                (
                    f"Coverage ID: {values['coverage id']}",
                    f"Title: {values['title']}",
                    f"Preconditions: {values['preconditions']}",
                    f"Steps: {values['steps']}",
                    f"Expected Result: {values['expected result']}",
                )
            )
        )
    return "\n\n".join(blocks)


def _plain_text_value(value: object, *, numbered: bool = False) -> str:
    if isinstance(value, list):
        parts = [str(item).strip() for item in value if str(item).strip()]
        if numbered:
            return " ".join(f"{index}. {part}" for index, part in enumerate(parts, start=1))
        return "; ".join(parts)
    return str(value).strip()


def _has_empty_field(block: str, pattern: re.Pattern[str]) -> bool:
    boundaries = sorted(
        match.start()
        for boundary in (*FIELD_PATTERNS.values(), COVERAGE_ID, CASE_HEADING)
        for match in boundary.finditer(block)
    )
    for match in pattern.finditer(block):
        end = next((start for start in boundaries if start >= match.end()), len(block))
        if not block[match.end() : end].strip():
            return True
    return False


PYTHON_PATH_MUTATION_METHODS = {
    "mkdir",
    "open",
    "rename",
    "replace",
    "touch",
    "unlink",
    "write_bytes",
    "write_text",
}
PYTHON_OS_MUTATION_METHODS = {
    "makedirs",
    "mkdir",
    "remove",
    "rename",
    "replace",
    "rmdir",
    "system",
    "unlink",
}
PYTHON_OS_PROCESS_METHODS = {
    "execl",
    "execle",
    "execlp",
    "execlpe",
    "execv",
    "execve",
    "execvp",
    "execvpe",
    "popen",
    "spawnl",
    "spawnle",
    "spawnlp",
    "spawnlpe",
    "spawnv",
    "spawnve",
    "spawnvp",
    "spawnvpe",
}
PYTHON_SHUTIL_MUTATION_METHODS = {
    "copy",
    "copy2",
    "copytree",
    "move",
    "rmtree",
}
PYTHON_SUBPROCESS_METHODS = {
    "Popen",
    "call",
    "check_call",
    "check_output",
    "run",
}
PYTHON_NETWORK_ROOTS = {"axios", "client", "httpx", "requests", "session"}
PYTHON_NETWORK_METHODS = {"delete", "patch", "post", "put", "request"}


PYTHON_EXTERNAL_WRITE_METHODS = (
    PYTHON_PATH_MUTATION_METHODS
    | PYTHON_OS_MUTATION_METHODS
    | PYTHON_OS_PROCESS_METHODS
    | PYTHON_SHUTIL_MUTATION_METHODS
    | PYTHON_SUBPROCESS_METHODS
    | PYTHON_NETWORK_METHODS
    | {"appendFile"}
)


def _contains_python_external_write(draft: str) -> bool:
    blocks = re.findall(r"```(?:python|py)?\s*\n?(.*?)```", draft, flags=re.IGNORECASE | re.DOTALL)
    candidates = blocks or [draft]
    for candidate in candidates:
        try:
            tree = ast.parse(candidate)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            path = _attribute_path(node.func)
            if not path:
                continue
            method = path[-1]
            if method not in PYTHON_EXTERNAL_WRITE_METHODS:
                continue
            if method == "open":
                if _open_writes(node):
                    return True
                continue
            if len(path) >= 2 and path[0] == "subprocess" and method in PYTHON_SUBPROCESS_METHODS:
                return True
            if len(path) >= 2 and path[0] == "os" and method in PYTHON_OS_MUTATION_METHODS:
                return True
            if len(path) >= 2 and path[0] == "os" and method in PYTHON_OS_PROCESS_METHODS:
                return True
            if len(path) >= 2 and path[0] == "shutil" and method in PYTHON_SHUTIL_MUTATION_METHODS:
                return True
            if method in PYTHON_PATH_MUTATION_METHODS and isinstance(node.func, ast.Attribute):
                if method == "replace" and not _is_path_like_receiver(node.func.value):
                    continue
                return True
            if method in PYTHON_NETWORK_METHODS and _is_network_call(node.func):
                if method == "request":
                    return _request_writes(node)
                return True
    return False


def _is_path_receiver(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    return _attribute_path(node.func) in (["Path"], ["pathlib", "Path"])


def _is_path_like_receiver(node: ast.AST) -> bool:
    if _is_path_receiver(node):
        return True
    if not isinstance(node, ast.Name):
        return False
    return any(
        marker in node.id.casefold() for marker in ("dest", "file", "output", "path", "target")
    )


def _is_network_call(node: ast.AST) -> bool:
    path = _attribute_path(node)
    if len(path) >= 2 and path[0] in PYTHON_NETWORK_ROOTS:
        return True
    if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Call):
        return False
    constructor = _attribute_path(node.value.func)
    return (
        len(constructor) >= 2
        and constructor[0] in {"axios", "httpx", "requests"}
        and constructor[-1] in {"AsyncClient", "Client", "Session"}
    )


def _open_writes(node: ast.Call) -> bool:
    mode: ast.AST | None = None
    path_receiver = isinstance(node.func, ast.Attribute) and _is_path_receiver(node.func.value)
    for keyword in node.keywords:
        if keyword.arg == "mode":
            mode = keyword.value
            break
    if mode is None:
        if (path_receiver or isinstance(node.func, ast.Attribute)) and node.args:
            mode = node.args[0]
        elif not path_receiver and len(node.args) > 1:
            mode = node.args[1]
        else:
            return False
    if not isinstance(mode, ast.Constant) or not isinstance(mode.value, str):
        return True
    return any(flag in mode.value for flag in ("w", "a", "x", "+"))


def _request_writes(node: ast.Call) -> bool:
    method_value: ast.AST | None = None
    for keyword in node.keywords:
        if keyword.arg == "method":
            method_value = keyword.value
            break
    if method_value is None and node.args:
        method_value = node.args[0]
    if not isinstance(method_value, ast.Constant) or not isinstance(method_value.value, str):
        return True
    return method_value.value.upper() in {"POST", "PUT", "PATCH", "DELETE"}


def _attribute_path(node: ast.AST) -> list[str]:
    path: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        path.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        path.append(current.id)
    return list(reversed(path))


def requested_case_count(text: str) -> int:
    separator = r"(?:-|\s+)"
    descriptors = r"(?:[\w-]+\s+){0,3}"
    case_noun = (
        r"(?:cases?|scenarios?|items?|(?:тест[- ])?кейс(?:а|ов)?|"
        r"сценар(?:ия|иев)|пункт(?:а|ов)?)\b"
    )
    numeric = re.search(
        rf"(?i)\b([1-9]\d*){separator}{descriptors}{case_noun}",
        text,
    )
    if numeric:
        return int(numeric.group(1))

    words = "|".join(map(re.escape, COUNT_WORDS))
    named = re.search(
        rf"(?i)\b({words}){separator}{descriptors}{case_noun}",
        text,
    )
    return COUNT_WORDS[named.group(1).lower()] if named else 1


def repair_instruction(issues: list[str]) -> str:
    descriptions = {
        **{
            f"test_cases_empty_{field}": f"provide non-empty {field} content in every test case"
            for field in FIELD_PATTERNS
        },
        "test_cases_missing_title": (
            "match the requested case count and start every case with Title:"
        ),
        "test_cases_wrong_count": "return exactly the requested number of test cases",
        "test_cases_missing_preconditions": "include Preconditions in every test case",
        "test_cases_missing_steps": "include Steps in every test case",
        "test_cases_missing_expected_result": ("include Expected Result in every test case"),
        "test_cases_duplicate_coverage_id": "return every supplied Coverage ID exactly once",
        "test_cases_unexpected_coverage_id": "remove Coverage IDs not supplied in the input",
        "test_cases_missing_coverage_id": "include every supplied Coverage ID exactly once",
        "short_explanation_too_long": "keep the explanation at or below 120 words",
        "translation_missing_preserve_term": "repeat every preserved term verbatim",
        "automation_external_write": "remove every external write operation",
        "review_missing_scope": "include the Scope section",
        "review_missing_checklist": "include the Checklist section",
        "review_missing_candidate_coverage_gaps": "include the Candidate Coverage Gaps section",
        "review_missing_positive_observations": "include the Positive Observations section",
        "review_missing_unverified": "include the Unverified section",
        "review_external_write": "remove every external write operation",
        "review_unsupported_decision": (
            "remove severity, priority, root cause, readiness, verdict, and confirmation claims"
        ),
    }
    requirements = [descriptions.get(issue, issue) for issue in issues]
    return (
        "REPAIR_REQUIRED: regenerate the complete artifact and fix these validation errors: "
        + "; ".join(requirements)
        + ". Keep draft as one plain-text string with literal headings; never put a nested JSON "
        "array or object inside draft. Return only JSON matching the schema."
    )
