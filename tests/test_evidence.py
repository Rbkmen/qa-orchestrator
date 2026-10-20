import hashlib
import json
import subprocess

import pytest
from pydantic import ValidationError

from qa_orchestrator.contracts import (
    EvidenceRecord,
    ProfileContextSelection,
    ProfileReviewReport,
    ReviewAgent,
    ReviewContext,
    ReviewFindingCandidate,
)
from qa_orchestrator.evidence import (
    _neighbor_categories,
    _path_tokens,
    discover_neighbor_evidence,
    evidence_main,
    register_evidence,
    validate_registered_locations,
    verify_evidence,
)
from qa_orchestrator.orchestration import DeepReviewSignals, OrchestrationError, QaOrchestrator


@pytest.fixture
def pinned_evidence(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()

    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(repository), *args], stderr=subprocess.PIPE
        )

    git("init", "--quiet")
    # Preserve the CRLF blob even when the host enables Git's line-ending conversion.
    (repository / ".gitattributes").write_bytes(b"src/api.py -text\n")
    (repository / "src").mkdir()
    source = repository / "src/api.py"
    source.write_bytes(b"first\r\noriginal\r\nlast")
    git("add", ".gitattributes", "src/api.py")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base")
    base = git("rev-parse", "HEAD").decode().strip()
    source.write_bytes(b"first\r\nchanged\r\nlast")
    git("add", "src/api.py")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "head")
    head = git("rev-parse", "HEAD").decode().strip()
    diff = git("diff", "--binary", "--no-ext-diff", "--no-textconv", base, head, "--")
    packet = tmp_path / "packet.md"
    packet.write_bytes(b"compact packet")
    context = ReviewContext(
        repository="github.com/example/repo",
        base_sha=base,
        head_sha=head,
        diff_sha256=hashlib.sha256(diff).hexdigest(),
        evidence_packet_sha256=hashlib.sha256(packet.read_bytes()).hexdigest(),
        changed_files=("src/api.py",),
        evidence_refs=("S1", "D1"),
    )
    definitions = [
        {
            "evidence_ref": "S1",
            "kind": "source",
            "revision": head,
            "path": "src/api.py",
            "start_line": 2,
            "end_line": 3,
        },
        {"evidence_ref": "D1", "kind": "diff", "revision": head},
    ]
    return repository, packet, context, definitions


def candidate(line=2, evidence_ref="S1"):
    return ReviewFindingCandidate(
        candidate_id="F-01",
        title="Candidate",
        description="Wrong result",
        confidence="high",
        verification_gap="No runtime check",
        evidence_refs=(evidence_ref,),
        source_locations=({"path": "src/api.py", "start_line": line},),
        failure_condition="The changed branch returns the wrong API value.",
        flip_condition="The pinned contract or caller proves the returned value is expected.",
    )


def test_legacy_source_record_keeps_its_wire_shape(pinned_evidence):
    _, _, context, _ = pinned_evidence
    record = EvidenceRecord(
        evidence_ref="S1",
        kind="source",
        revision=context.head_sha,
        path="src/api.py",
        sha256="0" * 64,
    )

    assert record.source_text_available is None
    assert "source_text_available" not in record.model_dump(mode="json")


def test_registry_hashes_exact_bytes_and_verifies_packet_and_locations(pinned_evidence):
    repository, packet, context, definitions = pinned_evidence
    bound = register_evidence(context, definitions, repository)
    assert bound.evidence_registry[0].sha256 == hashlib.sha256(b"changed\r\nlast").hexdigest()
    assert bound.evidence_registry[0].source_text_available is True
    assert bound.evidence_registry[1].source_text_available is None
    assert "source_text_available" not in bound.evidence_registry[1].model_dump(mode="json")
    result = verify_evidence(bound, repository, packet, (candidate(),))
    assert result["verified_refs"] == ["S1", "D1"]
    assert result["packet_hash_verified"] is True
    assert result["finding_locations_checked"] == 1


def test_content_packet_and_diff_mismatches_are_rejected(pinned_evidence):
    repository, packet, context, definitions = pinned_evidence
    bound = register_evidence(context, definitions, repository)
    data = bound.model_dump()
    data["evidence_registry"][0]["sha256"] = "f" * 64
    with pytest.raises(ValueError, match="content hash mismatch"):
        verify_evidence(ReviewContext.model_validate(data), repository)
    packet.write_bytes(b"different packet")
    with pytest.raises(ValueError, match="Packet hash mismatch"):
        verify_evidence(bound, repository, packet)
    wrong_diff = ReviewContext.model_validate({**context.model_dump(), "diff_sha256": "f" * 64})
    with pytest.raises(ValueError, match="bound diff hash"):
        register_evidence(wrong_diff, definitions, repository)


def test_changed_file_list_must_match_the_pinned_diff(pinned_evidence):
    repository, _, context, definitions = pinned_evidence
    wrong_paths = ReviewContext.model_validate(
        {**context.model_dump(), "changed_files": ("src/other.py",)}
    )
    with pytest.raises(ValueError, match="changed_files do not match"):
        register_evidence(wrong_paths, definitions, repository)


def test_claimed_lines_must_use_a_supporting_head_ref_and_stay_in_bounds(pinned_evidence):
    repository, _, context, definitions = pinned_evidence
    bound = register_evidence(context, definitions, repository)
    with pytest.raises(ValueError, match="registered head source"):
        validate_registered_locations(bound, (candidate(line=1),))
    with pytest.raises(ValueError, match="registered head source"):
        validate_registered_locations(bound, (candidate(evidence_ref="D1"),))
    definitions[0].pop("start_line")
    definitions[0].pop("end_line")
    entire_file = register_evidence(context, definitions, repository)
    with pytest.raises(ValueError, match="outside the pinned head"):
        verify_evidence(entire_file, repository, candidates=(candidate(line=99),))


def test_legacy_source_record_without_text_status_keeps_previous_location_behavior(
    pinned_evidence,
):
    repository, _, context, definitions = pinned_evidence
    bound = register_evidence(context, definitions, repository)
    legacy = ReviewContext.model_validate_json(bound.model_dump_json())
    record = legacy.evidence_registry[0].model_dump(mode="python")
    record.pop("source_text_available", None)
    legacy = ReviewContext.model_validate(
        {
            **legacy.model_dump(mode="python"),
            "evidence_registry": (record, *legacy.evidence_registry[1:]),
        }
    )

    assert legacy.evidence_registry[0].source_text_available is None
    validate_registered_locations(legacy, (candidate(),))


def test_legacy_text_source_session_can_finish_with_its_existing_location(
    pinned_evidence, make_final_review_report
):
    repository, _, context, definitions = pinned_evidence
    bound = register_evidence(context, definitions, repository)
    registry = [record.model_dump(mode="python") for record in bound.evidence_registry]
    registry[0].pop("source_text_available", None)
    legacy = ReviewContext.model_validate(
        {**bound.model_dump(mode="python"), "evidence_registry": registry}
    )
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    legacy_candidate = candidate().model_copy(
        update={
            "affected_area": "API response",
            "impact": "The caller can receive a wrong response.",
        }
    )
    session = orchestrator.start("ordinary_review", legacy)
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step="triage",
        status="completed",
        selected_profile=ReviewAgent.CODE_REVIEWER,
    )
    orchestrator.advance(
        run_id=session.run_id,
        completed_step=session.current_step,
        status="completed",
        completed_profile=ReviewAgent.CODE_REVIEWER,
        profile_reports=(
            ProfileReviewReport(
                profile=ReviewAgent.CODE_REVIEWER,
                context_selection=ProfileContextSelection(
                    source_packet_sha256=legacy.evidence_packet_sha256,
                    selected_packet_sha256="7" * 64,
                    included_refs=legacy.evidence_refs,
                    changed_files_count=1,
                    max_bytes=1024,
                    selected_bytes=256,
                ),
                scope="Pinned head source review",
                finding_candidates=(legacy_candidate,),
                reviewed_changed_paths=("src/api.py",),
            ),
        ),
        risk_signals=DeepReviewSignals(),
    )
    session = orchestrator.get(session.run_id)
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=session.current_step,
        status="completed",
    )
    final_report = make_final_review_report(
        (ReviewAgent.CODE_REVIEWER,),
        findings=((legacy_candidate, (ReviewAgent.CODE_REVIEWER,)),),
        context=legacy,
    )

    finished = orchestrator.finish(
        run_id=session.run_id,
        outcome="completed",
        final_review_report=final_report,
    )

    assert finished.final_review_report is not None
    assert finished.final_review_report.model_copy(update={"coverage_ledger": None}) == final_report


def test_binary_source_cannot_support_a_claimed_source_location(pinned_evidence):
    repository, _, context, definitions = pinned_evidence

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repository), *args])

    binary = repository / "assets/logo.png"
    binary.parent.mkdir()
    binary.write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff")
    git("add", "assets/logo.png")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "binary")
    head = git("rev-parse", "HEAD").decode().strip()
    diff = git("diff", "--binary", "--no-ext-diff", "--no-textconv", context.base_sha, head, "--")
    changed_files = tuple(
        item.decode("utf-8")
        for item in git("diff", "--name-only", "-z", context.base_sha, head, "--").split(b"\0")
        if item
    )
    updated_context = ReviewContext.model_validate(
        {
            **context.model_dump(),
            "head_sha": head,
            "diff_sha256": hashlib.sha256(diff).hexdigest(),
            "changed_files": changed_files,
            "evidence_refs": ("S1", "B1", "D1"),
        }
    )
    records = [
        {**definitions[0], "revision": head},
        {"evidence_ref": "B1", "kind": "source", "revision": head, "path": "assets/logo.png"},
        {"evidence_ref": "D1", "kind": "diff", "revision": head},
    ]
    bound = register_evidence(updated_context, records, repository)
    assert bound.evidence_registry[1].source_text_available is False
    binary_candidate = ReviewFindingCandidate(
        candidate_id="F-02",
        title="Binary location claim",
        description="The binary file has a line-level problem.",
        confidence="high",
        verification_gap="No runtime check",
        evidence_refs=("B1",),
        source_locations=({"path": "assets/logo.png", "start_line": 1},),
    )

    with pytest.raises(ValueError, match="registered head source evidence"):
        validate_registered_locations(bound, (binary_candidate,))

    legacy_payload = bound.model_dump(mode="python")
    legacy_payload["evidence_registry"] = tuple(
        {
            **record.model_dump(mode="python"),
            **({} if record.kind != "source" else {"source_text_available": None}),
        }
        for record in bound.evidence_registry
    )
    with pytest.raises(ValueError, match="registered head source evidence"):
        verify_evidence(
            ReviewContext.model_validate(legacy_payload),
            repository,
            candidates=(binary_candidate,),
        )

    tampered = bound.model_dump(mode="python")
    tampered["evidence_registry"][1]["source_text_available"] = True
    with pytest.raises(ValueError, match="source text availability metadata mismatch"):
        verify_evidence(ReviewContext.model_validate(tampered), repository)


def test_registry_is_complete_and_paths_are_repository_relative(pinned_evidence):
    repository, _, context, definitions = pinned_evidence
    with pytest.raises(ValidationError, match="map each registered ref"):
        register_evidence(context, definitions[:1], repository)
    for path in ("../escape", "/tmp/file", "C:/file", "src//file"):
        with pytest.raises(ValidationError, match="repository-relative"):
            EvidenceRecord(
                kind="source",
                evidence_ref="S1",
                revision=context.head_sha,
                sha256="0" * 64,
                path=path,
            )
    definitions[0]["path"] = "src"
    with pytest.raises(ValueError, match="file blob"):
        register_evidence(context, definitions, repository)


def test_server_rejects_candidate_coordinates_outside_registered_source(pinned_evidence):
    from qa_orchestrator.contracts import ProfileReviewReport

    repository, _, context, definitions = pinned_evidence
    bound = register_evidence(context, definitions, repository)
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review", bound)
    orchestrator.advance(
        run_id=session.run_id,
        completed_step="triage",
        status="completed",
        selected_profile="code_reviewer",
    )
    with pytest.raises(OrchestrationError, match="registered head source"):
        orchestrator.advance(
            run_id=session.run_id,
            completed_step="primary_review",
            status="completed",
            completed_profile="code_reviewer",
            risk_signals=DeepReviewSignals(),
            profile_reports=[
                ProfileReviewReport(
                    profile="code_reviewer", scope="Source", finding_candidates=(candidate(line=1),)
                )
            ],
        )
    assert orchestrator.get(session.run_id).completed_profiles == []


def test_external_evidence_remains_explicitly_unverified(pinned_evidence):
    repository, _, context, definitions = pinned_evidence
    context = ReviewContext.model_validate(
        {**context.model_dump(), "evidence_refs": ("S1", "D1", "R1")}
    )
    definitions.append(
        {"evidence_ref": "R1", "kind": "requirement", "locator": "TASK-123", "sha256": "f" * 64}
    )
    bound = register_evidence(context, definitions, repository)
    result = verify_evidence(bound, repository)
    assert result["unverified_external_refs"] == ["R1"]
    assert result["packet_hash_verified"] is None


def test_base_source_cannot_support_a_head_location(pinned_evidence):
    repository, _, context, definitions = pinned_evidence
    definitions[0]["revision"] = context.base_sha
    bound = register_evidence(context, definitions, repository)
    with pytest.raises(ValueError, match="registered head source"):
        validate_registered_locations(bound, (candidate(),))


def test_evidence_cli_builds_and_verifies_a_registry(tmp_path, pinned_evidence):
    import json

    repository, packet, context, definitions = pinned_evidence
    original, records, bound, output = (
        tmp_path / name
        for name in ("original.json", "records.json", "bound.json", "verification.json")
    )
    original.write_text(context.model_dump_json())
    records.write_text(json.dumps(definitions))
    assert (
        evidence_main(
            [
                "build",
                "--context",
                str(original),
                "--records",
                str(records),
                "--repository",
                str(repository),
                "--output",
                str(bound),
            ]
        )
        == 0
    )
    assert (
        evidence_main(
            [
                "verify",
                "--context",
                str(bound),
                "--repository",
                str(repository),
                "--packet",
                str(packet),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert json.loads(output.read_text())["verified_refs"] == ["S1", "D1"]


def test_discovery_suggests_pinned_callers_contracts_and_tests(tmp_path, monkeypatch):
    import qa_orchestrator.evidence as evidence_module

    monkeypatch.setattr(evidence_module, "_DISCOVERY_MAX_SYMBOL_SEARCH_PATHS", 3)
    repository = tmp_path / "repo"
    repository.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repository), *args])

    git("init", "--quiet")
    files = {
        "src/service.py": "def calculate(value):\n    return helper() + value + 1\n",
        "src/helper.py": "def helper():\n    return 1\n",
        "src/caller.py": "from src.service import calculate\nresult = calculate(2)\n",
        "src/irrelevant.py": "unrelated = True\n",
        "contracts/service_contract.py": "from src.service import calculate\n\nassert calculate(1) == 2\n",
        "tests/test_service.py": "from src.service import calculate\n\ndef test_calculate():\n    assert calculate(1) == 2\n",
    }
    for path, content in files.items():
        target = repository / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base")
    base = git("rev-parse", "HEAD").decode().strip()

    changed = repository / "src/service.py"
    changed.write_text("def calculate(value):\n    return helper() + value + 2\n", encoding="utf-8")
    git("add", "src/service.py")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "head")
    head = git("rev-parse", "HEAD").decode().strip()
    diff = git("diff", "--binary", "--no-ext-diff", "--no-textconv", base, head, "--")
    context = ReviewContext(
        repository="github.com/example/repo",
        base_sha=base,
        head_sha=head,
        diff_sha256=hashlib.sha256(diff).hexdigest(),
        evidence_packet_sha256=hashlib.sha256(b"packet").hexdigest(),
        changed_files=("src/service.py",),
        evidence_refs=("S1", "D1"),
    )

    original_run = evidence_module.subprocess.run
    stale_index = False
    codegraph_calls = []
    monkeypatch.setattr(
        evidence_module.shutil,
        "which",
        lambda command: "/fake/codegraph" if command == "codegraph" else None,
    )

    def fake_run(command, *args, **kwargs):
        nonlocal stale_index
        if command[0] != "/fake/codegraph":
            return original_run(command, *args, **kwargs)
        codegraph_calls.append(command)
        if command[1] == "status":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {
                        "initialized": True,
                        "projectPath": str(repository.resolve()),
                        "pendingChanges": {
                            "added": 0,
                            "modified": 1 if stale_index else 0,
                            "removed": 0,
                        },
                        "worktreeMismatch": None,
                        "index": {"state": "complete"},
                    }
                ),
                stderr="",
            )
        related_nodes = (
            [
                {
                    "name": "calculate",
                    "kind": "function",
                    "filePath": "src/caller.py",
                    "startLine": 2,
                }
            ]
            if command[1] == "callers"
            else [
                {
                    "name": "helper",
                    "kind": "function",
                    "filePath": "src/helper.py",
                    "startLine": 1,
                }
            ]
        )
        relation_data = {"callers" if command[1] == "callers" else "callees": related_nodes}
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "truncated": False,
                    "definitions": [
                        {
                            "definition": {
                                "filePath": "src/service.py",
                            },
                            "truncated": command[1] == "callees",
                            **relation_data,
                        }
                    ],
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(evidence_module.subprocess, "run", fake_run)
    manifest = discover_neighbor_evidence(context, repository, use_codegraph=True)
    assert manifest["codegraph"]["status"] == "ready"
    assert manifest["codegraph"]["symbols_checked"] == 1
    assert manifest["codegraph"]["caller_queries_checked"] == 1
    assert manifest["codegraph"]["callee_queries_checked"] == 1
    assert manifest["codegraph"]["truncated"] is True
    assert manifest["codegraph"]["caller_candidates"][0]["path"] == "src/caller.py"
    assert manifest["codegraph"]["callee_candidates"][0]["path"] == "src/helper.py"
    # Dirty worktree edits must not affect candidate hashes or line matches.
    caller = repository / "src/caller.py"
    caller.write_text("unrelated dirty worktree contents\n", encoding="utf-8")
    by_path = {entry["path"]: entry for entry in manifest["candidate_records"]}

    assert set(by_path) == {
        "contracts/service_contract.py",
        "src/helper.py",
        "src/caller.py",
        "tests/test_service.py",
    }
    assert "contract" in by_path["contracts/service_contract.py"]["categories"]
    assert "test" in by_path["tests/test_service.py"]["categories"]
    caller_entry = by_path["src/caller.py"]
    assert caller_entry["matched_symbols"] == [
        {"symbol": "calculate", "lines": [1, 2], "more_lines": False}
    ]
    assert "codegraph_caller_edge" in caller_entry["reasons"]
    assert caller_entry["codegraph_callers"][0]["start_line"] == 2
    assert "codegraph_callee_edge" in by_path["src/helper.py"]["reasons"]
    assert by_path["src/helper.py"]["codegraph_callees"][0]["start_line"] == 1
    pinned_caller = git("show", f"{head}:src/caller.py")
    assert caller_entry["sha256"] == hashlib.sha256(pinned_caller).hexdigest()
    assert caller_entry["record"]["revision"] == head
    assert manifest["exhaustive"] is False
    assert manifest["symbol_search_scope"] == "ranked_path_candidates"
    assert manifest["symbol_search_paths_considered"] == 3
    assert manifest["symbol_search_paths_omitted_by_limit"] >= 1
    assert manifest["symbol_search_exhaustive"] is False
    assert manifest["candidate_scan_limit"] == 100
    coverage = manifest["relationship_coverage"]
    assert coverage["exhaustive"] is False
    assert coverage["traced"][-1]["kind"] == "codegraph_callers_and_callees"
    assert coverage["traced"][-1]["truncated"] is True
    assert "event_names_subscribers_and_topics" in {
        item["kind"] for item in coverage["not_systematically_traced"]
    }
    omissions_by_kind = {item["kind"]: item for item in manifest["omissions"]}
    assert omissions_by_kind["symbol_search_path_limit"]["count"] >= 1
    assert omissions_by_kind["symbol_search_path_limit"]["examples"]

    previous_graph_calls = len(codegraph_calls)
    stale_index = True
    stale_manifest = discover_neighbor_evidence(context, repository, use_codegraph=True)
    assert stale_manifest["codegraph"]["status"] == "index_not_current"
    assert stale_manifest["codegraph"]["caller_candidates"] == []
    assert not any(command[1] == "callers" for command in codegraph_calls[previous_graph_calls:])

    context_path = tmp_path / "context.json"
    output_path = tmp_path / "neighbors.json"
    context_path.write_text(context.model_dump_json(), encoding="utf-8")
    assert evidence_main(
        [
            "discover",
            "--context",
            str(context_path),
            "--repository",
            str(repository),
            "--codegraph",
            "--output",
            str(output_path),
        ]
    ) == 0
    cli_manifest = json.loads(output_path.read_text(encoding="utf-8"))
    assert len(cli_manifest["candidate_records"]) == 3
    assert cli_manifest["codegraph"]["status"] == "index_not_current"


def test_discovery_reports_unavailable_optional_codegraph(pinned_evidence, monkeypatch):
    import qa_orchestrator.evidence as evidence_module

    repository, _, context, _ = pinned_evidence
    monkeypatch.setattr(evidence_module.shutil, "which", lambda _: None)

    manifest = discover_neighbor_evidence(context, repository, use_codegraph=True)

    assert manifest["codegraph"]["status"] == "not_installed"
    assert manifest["codegraph"]["caller_candidates"] == []
    assert manifest["exhaustive"] is False


def test_path_tokens_ignore_file_extensions():
    tokens = _path_tokens("src/components/LayerAtom.tsx")

    assert "layer" in tokens
    assert "atom" in tokens
    assert "tsx" not in tokens


@pytest.mark.parametrize(
    ("path", "category"),
    [
        ("src/api/checkout.py", "api"),
        ("db/migrations/001_add_status.sql", "database"),
        ("schemas/payment.proto", "contract"),
        ("events/deposit_published.py", "event"),
        ("workers/reconcile_orders.py", "job"),
        ("config/feature_flags.yml", "config"),
        ("authorization/policies/refunds.py", "authorization"),
    ],
)
def test_neighbor_categories_cover_system_boundaries(path, category):
    assert category in _neighbor_categories(path)


def test_discovery_follows_test_imports_and_suggests_dependency_manifest(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repository), *args])

    git("init", "--quiet")
    files = {
        "pyproject.toml": "[project.optional-dependencies]\ntest = ['pytest-mock', 'pytest-asyncio']\n",
        "src/fetching.py": (
            "async def send_request_to_open_ai(client, prompt):\n"
            "    return await client.send(prompt)\n\n"
            "def get_chat_completion(client, prompt):\n"
            "    return client.complete(prompt)\n\n"
            "def build_chat_response(completion):\n"
            "    return {'message': completion}\n\n"
            "def resolve_chat_request(client):\n"
            "    return client.complete()\n"
        ),
        "tests/test_fetching.py": (
            "from src.fetching import send_request_to_open_ai\n"
            "import src.fetching as fetching\n"
            "from unittest.mock import patch\n\n"
            "@patch('src.fetching.get_chat_completion')\n"
            "def test_request(mock_request, monkeypatch):\n"
            "    monkeypatch.setattr(fetching, 'build_chat_response', mock_request)\n"
            "    return fetching.resolve_chat_request(send_request_to_open_ai(mock_request))\n"
        ),
    }
    for path, content in files.items():
        target = repository / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base")
    base = git("rev-parse", "HEAD").decode().strip()

    changed = repository / "tests/test_fetching.py"
    changed.write_text(
        files["tests/test_fetching.py"].replace(
            "send_request_to_open_ai(mock_request)",
            "send_request_to_open_ai(mock_request, 'prompt')",
        ),
        encoding="utf-8",
    )
    git("add", "tests/test_fetching.py")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "head")
    head = git("rev-parse", "HEAD").decode().strip()
    diff = git("diff", "--binary", "--no-ext-diff", "--no-textconv", base, head, "--")
    context = ReviewContext(
        repository="github.com/example/repo",
        base_sha=base,
        head_sha=head,
        diff_sha256=hashlib.sha256(diff).hexdigest(),
        evidence_packet_sha256=hashlib.sha256(b"packet").hexdigest(),
        changed_files=("tests/test_fetching.py",),
        evidence_refs=("S1", "D1"),
    )

    manifest = discover_neighbor_evidence(context, repository)
    by_path = {entry["path"]: entry for entry in manifest["candidate_records"]}

    assert "send_request_to_open_ai" in manifest["referenced_symbols"]
    assert "get_chat_completion" in manifest["referenced_symbols"]
    assert "build_chat_response" in manifest["referenced_symbols"]
    assert "resolve_chat_request" in manifest["referenced_symbols"]
    assert "src/fetching.py" in by_path
    assert "send_request_to_open_ai" in {
        symbol["symbol"] for symbol in by_path["src/fetching.py"]["matched_symbols"]
    }
    assert by_path["src/fetching.py"]["categories"] == ["declaration"]
    assert "pyproject.toml" in by_path
    assert by_path["pyproject.toml"]["categories"] == ["dependency"]


def test_discovery_searches_local_module_import_paths_without_token_overlap(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repository), *args])

    git("init", "--quiet")
    files = {
        "src/auth_service.py": "def validate(user):\n    return bool(user)\n",
        "tests/test_login.py": (
            "from src.auth_service import validate\n\n"
            "def test_login():\n    assert validate('user')\n"
        ),
    }
    for path, content in files.items():
        target = repository / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base")
    base = git("rev-parse", "HEAD").decode().strip()

    changed_test = repository / "tests/test_login.py"
    changed_test.write_text(
        files["tests/test_login.py"].replace("validate('user')", "validate('')"),
        encoding="utf-8",
    )
    git("add", "tests/test_login.py")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "head")
    head = git("rev-parse", "HEAD").decode().strip()
    diff = git("diff", "--binary", "--no-ext-diff", "--no-textconv", base, head, "--")
    context = ReviewContext(
        repository="github.com/example/repo",
        base_sha=base,
        head_sha=head,
        diff_sha256=hashlib.sha256(diff).hexdigest(),
        evidence_packet_sha256=hashlib.sha256(b"packet").hexdigest(),
        changed_files=("tests/test_login.py",),
        evidence_refs=("S1", "D1"),
    )

    manifest = discover_neighbor_evidence(context, repository)

    by_path = {entry["path"]: entry for entry in manifest["candidate_records"]}
    assert "src/auth_service.py" in by_path
    assert "validate" in {item["symbol"] for item in by_path["src/auth_service.py"]["matched_symbols"]}


def test_discovery_follows_local_typescript_imports_to_action_contract(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repository), *args])

    git("init", "--quiet")
    files = {
        "src/Layer.tsx": (
            'import { layerAtom } from "./layerAtom";\n'
            "export function clearLayer() { return layerAtom.actions.clear(); }\n"
        ),
        "src/layerAtom.ts": (
            "export const layerAtom = atom({\n"
            "  actions: { clear: () => ({ type: 'clear' }) },\n"
            "});\n"
        ),
    }
    for path, content in files.items():
        target = repository / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base")
    base = git("rev-parse", "HEAD").decode().strip()

    changed = repository / "src/Layer.tsx"
    changed.write_text(files["src/Layer.tsx"].replace("clear()", "clear(prompt = 'confirm')"), encoding="utf-8")
    git("add", "src/Layer.tsx")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "head")
    head = git("rev-parse", "HEAD").decode().strip()
    diff = git("diff", "--binary", "--no-ext-diff", "--no-textconv", base, head, "--")
    context = ReviewContext(
        repository="github.com/example/repo",
        base_sha=base,
        head_sha=head,
        diff_sha256=hashlib.sha256(diff).hexdigest(),
        evidence_packet_sha256=hashlib.sha256(b"packet").hexdigest(),
        changed_files=("src/Layer.tsx",),
        evidence_refs=("S1", "D1"),
    )

    manifest = discover_neighbor_evidence(context, repository)
    by_path = {entry["path"]: entry for entry in manifest["candidate_records"]}

    assert "layerAtom" in manifest["referenced_symbols"]
    assert "src/layerAtom.ts" in by_path
    assert "layerAtom" in {
        symbol["symbol"] for symbol in by_path["src/layerAtom.ts"]["matched_symbols"]
    }


def test_discovery_finds_reverse_importers_and_exposes_test_audit_candidates(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repository), *args])

    git("init", "--quiet")
    files = {
        "src/engine/price.ts": (
            "export function calculatePrice(items: number[]) {\n"
            "  return items.reduce((sum, item) => sum + item, 0);\n"
            "}\n"
        ),
        "src/routes/cart.ts": (
            'import { calculatePrice as total } from "../engine/price";\n'
            "export function cartTotal(items: number[]) { return total(items); }\n"
        ),
        "tests/billing/price.test.ts": (
            'test("calculates the price", () => { calculatePrice([1, 2]); });\n'
        ),
    }
    for path, content in files.items():
        target = repository / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base")
    base = git("rev-parse", "HEAD").decode().strip()

    changed = repository / "src/engine/price.ts"
    changed_source = files["src/engine/price.ts"].replace("sum + item", "sum - item")
    changed_source += "\n".join(
        f"export function changedSymbol{index}() {{ return {index}; }}"
        for index in range(140)
    )
    changed.write_text(changed_source, encoding="utf-8")
    changed_test = repository / "tests/billing/price.test.ts"
    changed_test.write_text(
        files["tests/billing/price.test.ts"].replace(
            "calculates the price", "calculates the changed price"
        ),
        encoding="utf-8",
    )
    git("add", "src/engine/price.ts", "tests/billing/price.test.ts")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "head")
    head = git("rev-parse", "HEAD").decode().strip()
    diff = git("diff", "--binary", "--no-ext-diff", "--no-textconv", base, head, "--")
    context = ReviewContext(
        repository="github.com/example/repo",
        base_sha=base,
        head_sha=head,
        diff_sha256=hashlib.sha256(diff).hexdigest(),
        evidence_packet_sha256=hashlib.sha256(b"packet").hexdigest(),
        changed_files=("src/engine/price.ts", "tests/billing/price.test.ts"),
        evidence_refs=("S1", "D1"),
    )

    manifest = discover_neighbor_evidence(context, repository)

    by_path = {entry["path"]: entry for entry in manifest["candidate_records"]}
    assert "src/routes/cart.ts" in by_path
    assert "imports_changed_path" in by_path["src/routes/cart.ts"]["reasons"]
    assert by_path["src/routes/cart.ts"]["imported_changed_paths"] == [
        "src/engine/price.ts"
    ]
    assert manifest["test_audit"]["test_files_scanned"] == 1
    assert manifest["changed_sources_skipped_symbol_scan"] == 1
    assert len(manifest["test_audit"]["candidates"]) == 1
    audit_candidate = manifest["test_audit"]["candidates"][0]
    assert audit_candidate["category"] == "no_direct_assertion"
    assert audit_candidate["path"] == "tests/billing/price.test.ts"


def test_discovery_skips_symbol_search_when_neighbor_file_exceeds_byte_cap(
    tmp_path, monkeypatch
):
    import qa_orchestrator.evidence as evidence_module

    repository = tmp_path / "repo"
    repository.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repository), *args])

    git("init", "--quiet")
    files = {
        "src/service.py": "def calculate(value):\n    return value + 1\n",
        "src/large_caller.py": "from service import calculate\n" + "# filler\n" * 100,
    }
    for path, content in files.items():
        target = repository / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base")
    base = git("rev-parse", "HEAD").decode().strip()

    changed = repository / "src/service.py"
    changed.write_text(files["src/service.py"].replace("value + 1", "value + 2"), encoding="utf-8")
    git("add", "src/service.py")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "head")
    head = git("rev-parse", "HEAD").decode().strip()
    diff = git("diff", "--binary", "--no-ext-diff", "--no-textconv", base, head, "--")
    context = ReviewContext(
        repository="github.com/example/repo",
        base_sha=base,
        head_sha=head,
        diff_sha256=hashlib.sha256(diff).hexdigest(),
        evidence_packet_sha256=hashlib.sha256(b"packet").hexdigest(),
        changed_files=("src/service.py",),
        evidence_refs=("S1", "D1"),
    )
    monkeypatch.setattr(evidence_module, "_DISCOVERY_MAX_SOURCE_BYTES", 128)

    manifest = discover_neighbor_evidence(context, repository)

    assert manifest["symbol_search_candidate_paths"] == 1
    assert manifest["symbol_search_paths_considered"] == 0
    assert manifest["symbol_search_paths_skipped_by_size"] == 1
    assert manifest["candidate_records"] == []
