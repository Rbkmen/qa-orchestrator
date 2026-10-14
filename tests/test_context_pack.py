import hashlib
import json

import pytest
from test_evidence import pinned_evidence as pinned_evidence  # noqa: PLC0414

from qa_orchestrator.contracts import EvidenceRecord
from qa_orchestrator.evidence import register_evidence


def fragment(ref, text, *, required=True, profiles=(), priority=0):
    from qa_orchestrator.context_pack import ContextFragment

    return ContextFragment(
        record=EvidenceRecord(
            evidence_ref=ref,
            kind="other",
            locator="host:" + ref,
            sha256=hashlib.sha256(text.encode()).hexdigest(),
        ),
        text=text,
        required=required,
        profiles=profiles,
        priority=priority,
    )


def test_pack_is_deterministic_exact_and_filters_optional_profile_context():
    from qa_orchestrator.context_pack import ContextPacket, select_context

    required = fragment("E1", "first\r\nlast")
    optional = fragment("E2", "TEST_ONLY", required=False, profiles=("pr_test_analyzer",))
    packet = ContextPacket(fragments=(optional, required))
    packed = select_context(packet, "python_reviewer", max_bytes=2000)
    assert "first\r\nlast" in packed.text
    assert "TEST_ONLY" not in packed.text
    assert packed.included_refs == ("E1",)
    assert packed.omitted_refs == ("E2",)
    assert (
        select_context(
            ContextPacket(fragments=(required, optional)), "python_reviewer", max_bytes=2000
        )
        == packed
    )
    assert packed.sha256 == hashlib.sha256(packed.text.encode()).hexdigest()


def test_mandatory_overflow_fails_instead_of_summarizing_or_truncating():
    from qa_orchestrator.context_pack import ContextPacket, select_context

    packet = ContextPacket(fragments=(fragment("E1", "x" * 200),))
    with pytest.raises(ValueError, match="mandatory context"):
        select_context(packet, "python_reviewer", max_bytes=100)


def test_packet_rejects_tampered_content_and_duplicate_refs():
    from qa_orchestrator.context_pack import ContextPacket

    item = fragment("E1", "exact")
    bad = item.model_dump()
    bad["text"] = "modified"
    with pytest.raises(ValueError, match="content hash"):
        ContextPacket(fragments=(bad,))
    with pytest.raises(ValueError, match="unique"):
        ContextPacket(fragments=(item, item))


def test_dedup_preserves_aliases_and_optional_overflow_is_explicit():
    from qa_orchestrator.context_pack import ContextPacket, select_context

    first = fragment("E1", "UNIQUE_SOURCE_BYTES")
    alias = first.model_copy(
        update={"record": first.record.model_copy(update={"evidence_ref": "E2"})}
    )
    optional = fragment("E3", "z" * 2000, required=False)
    packed = select_context(
        ContextPacket(fragments=(first, alias, optional)), "python_reviewer", max_bytes=1000
    )
    assert packed.text.count("UNIQUE_SOURCE_BYTES") == 1
    assert packed.included_refs == ("E1", "E2")
    assert packed.omitted_refs == packed.budget_omitted_refs == ("E3",)


def test_selection_cannot_cite_an_omitted_ref(bound_review_context, review_finding_candidate):
    from qa_orchestrator.contracts import ProfileContextSelection, ProfileReviewReport
    from qa_orchestrator.evidence import validate_context_selection

    selection = ProfileContextSelection(
        source_packet_sha256=bound_review_context.evidence_packet_sha256,
        selected_packet_sha256="a" * 64,
        included_refs=("E2",),
        omitted_refs=("E1",),
        max_bytes=1000,
        selected_bytes=10,
    )
    report = ProfileReviewReport(
        profile="code_reviewer",
        scope="Inspected subset",
        context_selection=selection,
        finding_candidates=(review_finding_candidate,),
    )
    with pytest.raises(ValueError, match="omitted"):
        validate_context_selection(bound_review_context, report)


def test_cache_is_bounded_and_evicted_blob_reads_still_match_git(pinned_evidence):
    from qa_orchestrator.context_pack import PinnedSourceCache

    repo, _, context, definitions = pinned_evidence
    context = register_evidence(context, definitions, repo)
    entry = context.evidence_registry[0]
    cache = PinnedSourceCache(max_bytes=1)
    assert cache.read(repo, entry) == cache.read(repo, entry) == b"changed\r\nlast"
    assert cache.hits == 0


def test_source_builder_reads_git_and_cache_never_reads_dirty_worktree(pinned_evidence):
    from qa_orchestrator.context_pack import PinnedSourceCache, build_context_packet

    repo, _, context, definitions = pinned_evidence
    context = register_evidence(context, definitions, repo)
    cache = PinnedSourceCache(max_bytes=1000)
    first = build_context_packet(context, repo, cache=cache)
    (repo / "src/api.py").write_text("DIRTY")
    second = build_context_packet(context, repo, cache=cache)
    assert first == second
    assert cache.hits == 1
    source = next(f for f in first.fragments if f.record.evidence_ref == "S1")
    assert source.text == "changed\r\nlast"
    assert source.record.start_line == 2


def test_changed_source_cannot_be_marked_optional(pinned_evidence):
    from qa_orchestrator.context_pack import build_context_packet

    repo, _, context, definitions = pinned_evidence
    context = register_evidence(context, definitions, repo)
    with pytest.raises(ValueError, match="changed source"):
        build_context_packet(context, repo, optional_profiles={"S1": ("pr_test_analyzer",)})


@pytest.mark.parametrize("index", [0, 1])
def test_builder_rejects_missing_mandatory_artifact(pinned_evidence, index):
    from qa_orchestrator.context_pack import build_context_packet

    repo, _, context, definitions = pinned_evidence
    record = definitions[index]
    context = context.model_copy(update={"evidence_refs": (record["evidence_ref"],)})
    context = register_evidence(context, [record], repo)
    with pytest.raises(ValueError, match="mandatory"):
        build_context_packet(context, repo)


def test_modified_file_requires_head_source(pinned_evidence):
    from qa_orchestrator.context_pack import build_context_packet

    repo, _, context, definitions = pinned_evidence
    definitions[0]["revision"] = context.base_sha
    context = register_evidence(context, definitions, repo)
    with pytest.raises(ValueError, match="mandatory.*revision"):
        build_context_packet(context, repo)


def test_deleted_file_uses_verified_base_source(pinned_evidence):
    import subprocess

    from qa_orchestrator.context_pack import build_context_packet, validate_packet_context

    repo, _, context, definitions = pinned_evidence
    base = context.head_sha
    subprocess.run(["git", "-C", str(repo), "rm", "src/api.py"], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "delete",
        ],
        check=True,
    )
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"]).decode().strip()
    diff = subprocess.check_output(
        [
            "git",
            "-C",
            str(repo),
            "diff",
            "--binary",
            "--no-ext-diff",
            "--no-textconv",
            base,
            head,
            "--",
        ]
    )
    context = context.model_copy(
        update={"base_sha": base, "head_sha": head, "diff_sha256": hashlib.sha256(diff).hexdigest()}
    )
    definitions[0]["revision"], definitions[1]["revision"] = base, head
    context = register_evidence(context, definitions, repo)
    packet = build_context_packet(context, repo)
    assert packet.deleted_paths == ("src/api.py",)
    validate_packet_context(packet, context)


def test_pack_cli_binds_serialized_packet_hash(pinned_evidence, tmp_path):
    from qa_orchestrator.evidence import evidence_main

    repo, _, context, definitions = pinned_evidence
    context = register_evidence(context, definitions, repo)
    context_path, packet, bound = (
        tmp_path / name for name in ("in.json", "pack.json", "bound.json")
    )
    context_path.write_text(context.model_dump_json())
    assert (
        evidence_main(
            [
                "pack",
                "--context",
                str(context_path),
                "--repository",
                str(repo),
                "--output",
                str(packet),
                "--bound-context",
                str(bound),
            ]
        )
        == 0
    )
    document = json.loads(packet.read_text())
    assert document["format"] == "qa-orchestrator-context-v1"
    assert (
        json.loads(bound.read_text())["evidence_packet_sha256"]
        == hashlib.sha256(packet.read_bytes()).hexdigest()
    )
