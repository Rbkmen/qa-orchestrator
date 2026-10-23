import json
from urllib.error import HTTPError
from urllib.request import Request

from qa_orchestrator.contracts import EvidenceRecord, ReviewContext
from qa_orchestrator.evidence import evidence_main


def _record(locator="https://github.com/example/repo/pull/1"):
    return EvidenceRecord(
        evidence_ref="E1",
        kind="requirement",
        sha256="a" * 64,
        locator=locator,
    )


def _context(record):
    return ReviewContext(
        repository="github.com/example/repo",
        base_sha="1" * 40,
        head_sha="2" * 40,
        diff_sha256="3" * 64,
        evidence_packet_sha256="4" * 64,
        changed_files=("README.md",),
        evidence_refs=(record.evidence_ref,),
        evidence_registry=(record,),
    )


def test_check_evidence_links_reports_redirected_urls(monkeypatch):
    from qa_orchestrator import link_audit

    class Response:
        status = 200

        def getcode(self):
            return self.status

        def geturl(self):
            return "https://github.com/new-owner/repo/pull/1"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class Opener:
        def open(self, _request, timeout):
            assert timeout == 3
            return Response()

    monkeypatch.setattr(
        link_audit.urllib.request,
        "build_opener",
        lambda *_handlers: Opener(),
    )

    result = link_audit.check_evidence_links(
        (_record(),), allowed_hosts=("github.com",), timeout_seconds=3
    )

    assert result["results"][0]["status"] == "redirected"
    assert result["results"][0]["final_url"] == "https://github.com/new-owner/repo/pull/1"


def test_check_evidence_links_does_not_request_a_host_outside_the_allowlist(monkeypatch):
    from qa_orchestrator import link_audit

    class Opener:
        calls = 0

        def open(self, *_args, **_kwargs):
            self.calls += 1
            raise AssertionError("disallowed host must not be requested")

    opener = Opener()
    monkeypatch.setattr(
        link_audit.urllib.request,
        "build_opener",
        lambda *_handlers: opener,
    )

    result = link_audit.check_evidence_links(
        (_record("https://outside.example/repo/pull/1"),),
        allowed_hosts=("github.com",),
    )

    assert opener.calls == 0
    assert result["results"][0]["status"] == "host_not_allowed"


def test_check_evidence_links_classifies_not_found(monkeypatch):
    from qa_orchestrator import link_audit

    class Opener:
        def open(self, request, timeout):
            raise HTTPError(request.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(
        link_audit.urllib.request,
        "build_opener",
        lambda *_handlers: Opener(),
    )

    result = link_audit.check_evidence_links((_record(),), allowed_hosts=("github.com",))

    assert result["results"][0]["status"] == "not_found"
    assert result["results"][0]["http_status"] == 404


def test_check_evidence_links_redacts_credentials_and_query_without_requesting(
    monkeypatch,
):
    from qa_orchestrator import link_audit

    class Opener:
        def open(self, *_args, **_kwargs):
            raise AssertionError("unsafe locators must not be requested")

    monkeypatch.setattr(
        link_audit.urllib.request,
        "build_opener",
        lambda *_handlers: Opener(),
    )

    result = link_audit.check_evidence_links(
        (_record("https://user:secret@github.com/example/repo/pull/1?token=secret"),),
        allowed_hosts=("github.com",),
    )

    assert result["results"][0]["status"] == "unsafe_locator"
    assert result["results"][0]["locator"] == "https://github.com/example/repo/pull/1"
    assert "secret" not in json.dumps(result)


def test_redirect_handler_blocks_hosts_outside_the_allowlist():
    from qa_orchestrator.link_audit import _AllowlistedRedirectHandler

    handler = _AllowlistedRedirectHandler(frozenset({"github.com"}))
    redirected = handler.redirect_request(
        Request("https://github.com/example/repo/pull/1"),
        None,
        302,
        "Found",
        {},
        "https://internal.example/admin",
    )

    assert redirected is None
    assert handler.blocked_redirect


def test_check_links_cli_writes_report_and_keeps_network_check_explicit(tmp_path, monkeypatch):
    from qa_orchestrator import link_audit

    record = _record()
    context_path = tmp_path / "context.json"
    output_path = tmp_path / "link-audit.json"
    context_path.write_text(_context(record).model_dump_json(), encoding="utf-8")

    class Response:
        status = 200

        def getcode(self):
            return self.status

        def geturl(self):
            return "https://github.com/example/repo/pull/1"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class Opener:
        def open(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(
        link_audit.urllib.request,
        "build_opener",
        lambda *_handlers: Opener(),
    )

    result = evidence_main(
        [
            "check-links",
            "--context",
            str(context_path),
            "--allow-host",
            "github.com",
            "--timeout",
            "3",
            "--output",
            str(output_path),
        ]
    )

    assert result == 0
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["results"][0]["evidence_refs"] == ["E1"]
    assert payload["results"][0]["status"] == "reachable"
