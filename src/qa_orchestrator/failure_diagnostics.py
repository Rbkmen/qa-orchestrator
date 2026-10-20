"""Small, bounded diagnostics safe to retain in host execution artifacts."""

import re

MAX_FAILURE_DETAIL_CHARS = 512

_CODEX_ERROR_CODES = frozenset(
    {
        "contextWindowExceeded",
        "sessionBudgetExceeded",
        "usageLimitExceeded",
        "rateLimitExceeded",
        "flexUnavailable",
        "serverOverloaded",
        "cyberPolicy",
        "misalignmentPolicyViolation",
        "tooManyDenials",
        "internalServerError",
        "unauthorized",
        "badRequest",
        "threadRollbackFailed",
        "sandboxError",
        "other",
    }
)
_CODEX_ERROR_VARIANTS = frozenset(
    {
        "httpConnectionFailed",
        "responseStreamConnectionFailed",
        "responseStreamDisconnected",
        "responseTooManyFailedAttempts",
        "activeTurnNotSteerable",
    }
)


def sanitize_failure_detail(text: str) -> str:
    """Keep one concise error line while masking common secrets and local paths."""
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    detail = text.splitlines()[0].strip() if text.splitlines() else ""
    detail = re.sub(
        r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+",
        "Bearer [REDACTED]",
        detail,
    )
    detail = re.sub(r"\b(?:sk|rk)-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", detail)
    detail = re.sub(
        r"(?i)\b(api[_ -]?key|(?:[a-z0-9]+[_ -])*(?:key|token|password|secret|credential))\b\s*[:=]\s*[^,\s;]+",
        r"\1=[REDACTED]",
        detail,
    )
    detail = re.sub(r"(?:/Users|/private|/tmp|/home)/[^\s,;]+", "[path]", detail)
    detail = re.sub(r"(?i)\b[A-Z]:\\Users\\[^\s,;]+", "[path]", detail)
    detail = re.sub(r"[\x00-\x1f\x7f]", " ", detail)
    return detail[:MAX_FAILURE_DETAIL_CHARS]


def safe_cli_failure_detail(stderr: bytes) -> str | None:
    """Return only the last bounded stderr error line, never the raw buffer."""
    text = stderr.decode("utf-8", errors="replace")
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    error_lines = [
        line.strip()
        for line in text.splitlines()
        if re.search(r"(^|\s)(error|fatal)(:|\b)", line, re.IGNORECASE)
    ]
    if error_lines:
        return sanitize_failure_detail(error_lines[-1])
    return None


def safe_cli_event_failure_detail(event: dict) -> str | None:
    """Extract only an allowlisted Codex error code and HTTP status from an event."""
    if event.get("type") not in {"error", "turn.failed"}:
        return None
    error = event.get("error")
    if not isinstance(error, dict):
        return None
    error_info = error.get("codexErrorInfo")
    if isinstance(error_info, str):
        if error_info in _CODEX_ERROR_CODES:
            return f"codex_error={error_info}"
        return None
    if not isinstance(error_info, dict):
        return None
    variants = [name for name in _CODEX_ERROR_VARIANTS if name in error_info]
    if len(variants) != 1:
        return None
    name = variants[0]
    detail = f"codex_error={name}"
    variant_data = error_info[name]
    status = variant_data.get("httpStatusCode") if isinstance(variant_data, dict) else None
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        detail += f" http_status={status}"
    return detail
