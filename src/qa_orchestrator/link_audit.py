"""Explicit, host-allowlisted checks for external evidence URLs."""

from __future__ import annotations

import ipaddress
import re
import ssl
import urllib.error
import urllib.request
from collections import Counter
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler

from qa_orchestrator.contracts import EvidenceRecord

_FAILED_STATUSES = frozenset(
    {
        "access_denied",
        "blocked_redirect",
        "host_not_allowed",
        "http_error",
        "invalid_url",
        "network_error",
        "not_found",
        "rate_limited",
        "server_error",
        "unsupported_scheme",
        "unsafe_locator",
    }
)


def _normalize_allowed_hosts(hosts: tuple[str, ...]) -> frozenset[str]:
    if not hosts:
        raise ValueError("at least one --allow-host is required for link checks")

    normalized: set[str] = set()
    for host in hosts:
        if not isinstance(host, str) or not host or host != host.strip() or "*" in host:
            raise ValueError("allowed hosts must be exact hostnames without wildcards")
        try:
            parsed = urlsplit(f"//{host}")
            hostname = parsed.hostname
            port = parsed.port
        except ValueError as exc:
            raise ValueError("allowed host is malformed") from exc
        if (
            hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or port is not None
        ):
            raise ValueError("allowed hosts must be exact hostnames without ports or paths")
        hostname = hostname.lower().rstrip(".")
        if (
            "." not in hostname
            or len(hostname) > 253
            or any(not label for label in hostname.split("."))
            or any(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is None
                for label in hostname.split(".")
            )
        ):
            raise ValueError("allowed host is malformed")
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            pass
        else:
            raise ValueError("IP addresses cannot be used as allowed hosts")
        normalized.add(hostname)
    return frozenset(normalized)


def _safe_report_url(locator: str) -> str | None:
    try:
        parsed = urlsplit(locator)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if not parsed.scheme or not hostname:
        return None
    netloc = hostname.lower()
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path, "", ""))


def _validate_url(locator: str, allowed_hosts: frozenset[str]) -> str | None:
    if not isinstance(locator, str) or not locator or locator != locator.strip():
        return "invalid_url"
    if any(
        character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
        for character in locator
    ):
        return "invalid_url"
    try:
        parsed = urlsplit(locator)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return "invalid_url"
    if not parsed.scheme or not parsed.netloc or not hostname:
        return "not_url"
    if parsed.scheme.lower() != "https":
        return "unsupported_scheme"
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return "unsafe_locator"
    if port not in (None, 443):
        return "unsafe_locator"
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        return "unsafe_locator"
    if hostname.lower().rstrip(".") not in allowed_hosts:
        return "host_not_allowed"
    return None


class _AllowlistedRedirectHandler(HTTPRedirectHandler):
    def __init__(self, allowed_hosts: frozenset[str]) -> None:
        super().__init__()
        self.allowed_hosts = allowed_hosts
        self.redirect_chain: list[str] = []
        self.blocked_redirect = False

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _validate_url(newurl, self.allowed_hosts) is not None:
            self.blocked_redirect = True
            return None
        self.redirect_chain.append(_safe_report_url(newurl) or "")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _https_context() -> ssl.SSLContext:
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def _http_status_name(status: int) -> str:
    if status in {404, 410}:
        return "not_found"
    if status in {401, 403}:
        return "access_denied"
    if status == 429:
        return "rate_limited"
    if status >= 500:
        return "server_error"
    return "http_error"


def _check_one_url(locator: str, allowed_hosts: frozenset[str], timeout_seconds: float) -> dict:
    safe_locator = _safe_report_url(locator)
    invalid_status = _validate_url(locator, allowed_hosts)
    if invalid_status is not None:
        return {
            "locator": safe_locator,
            "status": invalid_status,
            "http_status": None,
            "final_url": None,
            "redirect_chain": [],
            "error_type": None,
        }

    redirect_handler = _AllowlistedRedirectHandler(allowed_hosts)
    opener = urllib.request.build_opener(
        redirect_handler,
        urllib.request.HTTPSHandler(context=_https_context()),
    )
    request = urllib.request.Request(
        locator,
        headers={"User-Agent": "qa-orchestrator-link-audit/1.0", "Range": "bytes=0-0"},
    )
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            status = response.getcode()
            final_url = _safe_report_url(response.geturl())
            redirected = bool(redirect_handler.redirect_chain) or final_url != safe_locator
            return {
                "locator": safe_locator,
                "status": "redirected" if redirected else "reachable",
                "http_status": status,
                "final_url": final_url,
                "redirect_chain": redirect_handler.redirect_chain,
                "error_type": None,
            }
    except urllib.error.HTTPError as exc:
        status = (
            "blocked_redirect" if redirect_handler.blocked_redirect else _http_status_name(exc.code)
        )
        return {
            "locator": safe_locator,
            "status": status,
            "http_status": exc.code,
            "final_url": _safe_report_url(exc.geturl()),
            "redirect_chain": redirect_handler.redirect_chain,
            "error_type": type(exc).__name__,
        }
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return {
            "locator": safe_locator,
            "status": "network_error",
            "http_status": None,
            "final_url": None,
            "redirect_chain": redirect_handler.redirect_chain,
            "error_type": type(exc).__name__,
        }


def check_evidence_links(
    records: tuple[EvidenceRecord, ...] | list[EvidenceRecord],
    *,
    allowed_hosts: tuple[str, ...],
    timeout_seconds: float = 10.0,
) -> dict:
    """Check HTTPS locators only when the host is explicitly allowlisted."""
    if not 0 < timeout_seconds <= 60:
        raise ValueError("link-check timeout must be greater than 0 and at most 60 seconds")
    hosts = _normalize_allowed_hosts(allowed_hosts)
    groups: dict[str, list[str]] = {}
    results: list[dict] = []
    for record in records:
        if not record.locator:
            results.append(
                {
                    "evidence_refs": [record.evidence_ref],
                    "locator": None,
                    "status": "missing_locator",
                    "http_status": None,
                    "final_url": None,
                    "redirect_chain": [],
                    "error_type": None,
                }
            )
        elif "://" not in record.locator:
            results.append(
                {
                    "evidence_refs": [record.evidence_ref],
                    "locator": None,
                    "status": "not_url",
                    "http_status": None,
                    "final_url": None,
                    "redirect_chain": [],
                    "error_type": None,
                }
            )
        else:
            groups.setdefault(record.locator, []).append(record.evidence_ref)

    for locator, evidence_refs in groups.items():
        result = _check_one_url(locator, hosts, timeout_seconds)
        result["evidence_refs"] = evidence_refs
        results.append(result)

    counts = Counter(result["status"] for result in results)
    return {
        "format": "qa-orchestrator-link-audit-v1",
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "allowed_hosts": sorted(hosts),
        "summary": dict(sorted(counts.items())),
        "results": results,
    }


def link_audit_failed(result: dict) -> bool:
    return any(status in _FAILED_STATUSES for status in result.get("summary", {}))
