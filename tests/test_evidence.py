import hashlib
import json
import subprocess

import pytest
from pydantic import ValidationError

from qa_orchestrator.contracts import EvidenceRecord, ReviewContext, ReviewFindingCandidate
from qa_orchestrator.evidence import (
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
    )


def test_registry_hashes_exact_bytes_and_verifies_packet_and_locations(pinned_evidence):
    repository, packet, context, definitions = pinned_evidence
    bound = register_evidence(context, definitions, repository)
    assert bound.evidence_registry[0].sha256 == hashlib.sha256(b"changed\r\nlast").hexdigest()
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


def test_discovery_suggests_pinned_callers_contracts_and_tests(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repository), *args])

    git("init", "--quiet")
    files = {
        "src/service.py": "def calculate(value):\n    return value + 1\n",
        "src/caller.py": "from src.service import calculate\nresult = calculate(2)\n",
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
    changed.write_text("def calculate(value):\n    return value + 2\n", encoding="utf-8")
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

    # Dirty worktree edits must not affect candidate hashes or line matches.
    caller = repository / "src/caller.py"
    caller.write_text("unrelated dirty worktree contents\n", encoding="utf-8")
    manifest = discover_neighbor_evidence(context, repository)
    by_path = {entry["path"]: entry for entry in manifest["candidate_records"]}

    assert set(by_path) == {
        "contracts/service_contract.py",
        "src/caller.py",
        "tests/test_service.py",
    }
    assert "contract" in by_path["contracts/service_contract.py"]["categories"]
    assert "test" in by_path["tests/test_service.py"]["categories"]
    caller_entry = by_path["src/caller.py"]
    assert caller_entry["matched_symbols"] == [
        {"symbol": "calculate", "lines": [1, 2], "more_lines": False}
    ]
    pinned_caller = git("show", f"{head}:src/caller.py")
    assert caller_entry["sha256"] == hashlib.sha256(pinned_caller).hexdigest()
    assert caller_entry["record"]["revision"] == head
    assert manifest["exhaustive"] is False

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
            "--output",
            str(output_path),
        ]
    ) == 0
    assert len(json.loads(output_path.read_text(encoding="utf-8"))["candidate_records"]) == 3
