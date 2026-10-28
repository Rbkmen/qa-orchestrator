import hashlib
import json

import pytest
from test_evidence import pinned_evidence as pinned_evidence  # noqa: PLC0414

from qa_orchestrator.contracts import (
    EvidenceRecord,
    ProfileContextSelection,
    ProfileReviewReport,
    ReviewAgent,
)
from qa_orchestrator.evidence import register_evidence, validate_context_selection


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


def test_required_refs_bypass_profile_filters_and_fail_if_budget_cannot_fit():
    from qa_orchestrator.context_pack import ContextPacket, select_context

    optional = fragment(
        "H1", "prior review summary", required=False, profiles=(ReviewAgent.PR_TEST_ANALYZER,)
    )
    packet = ContextPacket(fragments=(optional,))

    packed = select_context(
        packet,
        ReviewAgent.PYTHON_REVIEWER,
        max_bytes=2000,
        required_refs=("H1",),
    )
    assert packed.included_refs == ("H1",)
    assert packed.omitted_refs == ()

    with pytest.raises(ValueError, match="mandatory context exceeds budget"):
        select_context(
            packet,
            ReviewAgent.PYTHON_REVIEWER,
            max_bytes=5,
            required_refs=("H1",),
        )


def test_packet_rejects_tampered_content_and_duplicate_refs():
    from qa_orchestrator.context_pack import ContextPacket

    item = fragment("E1", "exact")
    bad = item.model_dump()
    bad["text"] = "modified"
    with pytest.raises(ValueError, match="content hash"):
        ContextPacket(fragments=(bad,))
    with pytest.raises(ValueError, match="unique"):
        ContextPacket(fragments=(item, item))


def test_source_text_status_must_match_omission_marker_and_excerpt():
    from qa_orchestrator.context_pack import ContextFragment

    omitted_text = (
        "[Source content omitted from text context (binary or non-UTF-8); "
        f"sha256={'a' * 64}]"
    )
    record = EvidenceRecord(
        evidence_ref="S1",
        kind="source",
        revision="1" * 40,
        path="assets/logo.png",
        sha256="a" * 64,
        source_text_available=True,
    )
    with pytest.raises(ValueError, match="omission marker conflicts"):
        ContextFragment(record=record, text=omitted_text)

    legacy_record = record.model_copy(update={"source_text_available": None})
    ContextFragment(record=legacy_record, text=omitted_text)

    unavailable_record = record.model_copy(update={"source_text_available": False})
    excerpt = "actual source text"
    unavailable_record = unavailable_record.model_copy(
        update={"sha256": hashlib.sha256(excerpt.encode()).hexdigest()}
    )
    with pytest.raises(ValueError, match="marked unavailable"):
        ContextFragment(record=unavailable_record, text=excerpt)


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


def test_changed_source_can_be_omitted_by_profile_or_budget(pinned_evidence):
    from qa_orchestrator.context_pack import (
        ContextPacket,
        build_context_packet,
        select_context,
        validate_packet_context,
    )

    repo, _, context, definitions = pinned_evidence
    context = register_evidence(context, definitions, repo)
    packet = build_context_packet(
        context,
        repo,
        optional_profiles={"S1": (ReviewAgent.CODE_REVIEWER,)},
    )
    validate_packet_context(packet, context)
    source_fragment = next(fragment for fragment in packet.fragments if fragment.record.evidence_ref == "S1")
    assert not source_fragment.required

    diff_fragment = next(fragment for fragment in packet.fragments if fragment.record.evidence_ref == "D1")
    diff_only = ContextPacket(fragments=(diff_fragment,), deleted_paths=packet.deleted_paths)
    budget = len(select_context(diff_only, ReviewAgent.CODE_REVIEWER, max_bytes=2_000_000).text.encode())
    selected = select_context(packet, ReviewAgent.CODE_REVIEWER, max_bytes=budget)
    assert "D1" in selected.included_refs
    assert "S1" in selected.budget_omitted_refs

    shared = select_context(
        packet,
        ReviewAgent.CODE_EXPLORER,
        max_bytes=2_000_000,
        profile_override_refs=("S1",),
    )
    assert "S1" in shared.included_refs
    assert "S1" not in shared.omitted_refs

    shared_budget_limited = select_context(
        packet,
        ReviewAgent.CODE_EXPLORER,
        max_bytes=budget,
        profile_override_refs=("S1",),
    )
    assert "S1" in shared_budget_limited.budget_omitted_refs


def test_mr_claim_evidence_cannot_be_made_optional(pinned_evidence):
    from qa_orchestrator.context_pack import (
        ContextPacket,
        build_context_packet,
        validate_packet_context,
    )

    repo, _, context, definitions = pinned_evidence
    claim_text = "The MR says the endpoint preserves the account identifier."
    context = context.model_copy(update={"evidence_refs": (*context.evidence_refs, "C1")})
    bound = register_evidence(
        context,
        [
            *definitions,
            {
                "evidence_ref": "C1",
                "kind": "claim",
                "locator": "MR!17:description#2",
                "sha256": hashlib.sha256(claim_text.encode()).hexdigest(),
            },
        ],
        repo,
    )

    with pytest.raises(ValueError, match="MR claims must remain mandatory"):
        build_context_packet(
            bound,
            repo,
            optional_profiles={"C1": (ReviewAgent.CODE_REVIEWER,)},
            external_content={"C1": claim_text},
        )

    packet = build_context_packet(bound, repo, external_content={"C1": claim_text})
    forged = ContextPacket(
        fragments=tuple(
            fragment.model_copy(update={"required": False})
            if fragment.record.evidence_ref == "C1"
            else fragment
            for fragment in packet.fragments
        ),
        deleted_paths=packet.deleted_paths,
    )
    with pytest.raises(ValueError, match="mandatory diff/requirements/MR claims"):
        validate_packet_context(forged, bound)

    omitted_claim = ProfileReviewReport(
        profile=ReviewAgent.CODE_REVIEWER,
        context_selection=ProfileContextSelection(
            source_packet_sha256=bound.evidence_packet_sha256,
            selected_packet_sha256="8" * 64,
            included_refs=tuple(ref for ref in bound.evidence_refs if ref != "C1"),
            omitted_refs=("C1",),
            changed_files_count=len(bound.changed_files),
            max_bytes=2000,
            selected_bytes=1000,
        ),
        scope="Reviewed without the MR claim.",
    )
    with pytest.raises(ValueError, match="context selection omits mandatory evidence"):
        validate_context_selection(bound, omitted_claim)


def test_packet_marks_binary_source_as_omitted_and_keeps_raw_hash(pinned_evidence):
    import subprocess

    from qa_orchestrator.context_pack import (
        _omitted_source_text,
        build_context_packet,
        select_context,
        validate_packet_context,
    )
    from qa_orchestrator.contracts import ReviewContext, review_diff_hunk_inventory_sha256

    repo, _, context, _ = pinned_evidence
    binary_path = repo / "assets/logo.png"
    binary_path.parent.mkdir()
    binary_path.write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff")
    subprocess.check_call(["git", "-C", str(repo), "add", "assets/logo.png"])
    subprocess.check_call(
        [
            "git", "-C", str(repo), "-c", "user.name=Test", "-c",
            "user.email=test@example.invalid", "commit", "-qm", "add binary",
        ]
    )
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"]).decode().strip()
    diff = subprocess.check_output(
        ["git", "-C", str(repo), "diff", "--binary", context.base_sha, head, "--"]
    )
    context = ReviewContext.model_validate(
        {
            **context.model_dump(),
            "head_sha": head,
            "diff_sha256": hashlib.sha256(diff).hexdigest(),
            "diff_hunk_inventory_sha256": review_diff_hunk_inventory_sha256(
                hashlib.sha256(diff).hexdigest(), context.diff_hunks
            ),
            "changed_files": ("src/api.py", "assets/logo.png"),
            "evidence_refs": ("S1", "B1", "D1"),
        }
    )
    definitions = [
        {"evidence_ref": "S1", "kind": "source", "revision": head, "path": "src/api.py"},
        {"evidence_ref": "B1", "kind": "source", "revision": head, "path": "assets/logo.png"},
        {"evidence_ref": "D1", "kind": "diff", "revision": head},
    ]
    bound = register_evidence(context, definitions, repo)

    packet = build_context_packet(bound, repo)
    binary_fragment = next(f for f in packet.fragments if f.record.evidence_ref == "B1")
    assert binary_fragment.text == _omitted_source_text(binary_fragment.record.sha256)
    assert binary_fragment.record.sha256 == hashlib.sha256(binary_path.read_bytes()).hexdigest()
    assert binary_fragment.record.model_dump().get("source_text_available") is False
    validate_packet_context(packet, bound)
    packed = select_context(packet, "python_reviewer", max_bytes=2_000_000)
    assert binary_fragment.text in packed.text


def test_packet_size_limit_stops_before_reading_later_fragments(
    pinned_evidence, monkeypatch
):
    from qa_orchestrator import context_pack
    from qa_orchestrator.context_pack import PinnedSourceCache, build_context_packet

    repo, _, context, definitions = pinned_evidence
    large_text = "x" * 3000
    extra_records = [
        {
            "evidence_ref": "R1",
            "kind": "requirement",
            "locator": "host:requirement",
            "sha256": hashlib.sha256(large_text.encode()).hexdigest(),
        },
        {
            "evidence_ref": "S2",
            "kind": "source",
            "revision": context.head_sha,
            "path": "src/api.py",
        },
    ]
    context = context.model_copy(
        update={"evidence_refs": ("S1", "D1", "R1", "S2")}
    )
    bound = register_evidence(context, [*definitions, *extra_records], repo)
    monkeypatch.setattr(context_pack, "MAX_CONTEXT_PACKET_BYTES", 2048, raising=False)

    class TrackingCache(PinnedSourceCache):
        def __init__(self):
            super().__init__()
            self.read_paths = []

        def read(self, repository, entry):
            self.read_paths.append(entry.path)
            return super().read(repository, entry)

    cache = TrackingCache()
    with pytest.raises(ValueError, match="context packet exceeds executor input size"):
        build_context_packet(
            bound,
            repo,
            cache=cache,
            external_content={"R1": large_text},
        )
    assert cache.read_paths == ["src/api.py"]


def test_packet_limit_matches_exact_serialized_size(pinned_evidence, monkeypatch):
    from qa_orchestrator import context_pack
    from qa_orchestrator.context_pack import build_context_packet

    repo, _, context, definitions = pinned_evidence
    bound = register_evidence(context, definitions, repo)
    packet = build_context_packet(bound, repo)
    serialized_size = len(packet.model_dump_json().encode("utf-8"))
    monkeypatch.setattr(context_pack, "MAX_CONTEXT_PACKET_BYTES", serialized_size)

    assert build_context_packet(bound, repo) == packet

    monkeypatch.setattr(context_pack, "MAX_CONTEXT_PACKET_BYTES", serialized_size - 1)
    with pytest.raises(ValueError, match="context packet exceeds executor input size"):
        build_context_packet(bound, repo)


def test_builder_requires_diff_but_allows_missing_changed_source(pinned_evidence):
    from qa_orchestrator.context_pack import build_context_packet

    repo, _, context, definitions = pinned_evidence
    diff_record = definitions[1]
    diff_only_context = context.model_copy(update={"evidence_refs": (diff_record["evidence_ref"],)})
    diff_only_context = register_evidence(diff_only_context, [diff_record], repo)
    packet = build_context_packet(diff_only_context, repo)
    assert [fragment.record.evidence_ref for fragment in packet.fragments] == ["D1"]

    source_record = definitions[0]
    source_only_context = context.model_copy(update={"evidence_refs": (source_record["evidence_ref"],)})
    source_only_context = register_evidence(source_only_context, [source_record], repo)
    with pytest.raises(ValueError, match="mandatory diff"):
        build_context_packet(source_only_context, repo)


def test_modified_file_requires_head_source(pinned_evidence):
    from qa_orchestrator.context_pack import build_context_packet

    repo, _, context, definitions = pinned_evidence
    definitions[0]["revision"] = context.base_sha
    context = register_evidence(context, definitions, repo)
    with pytest.raises(ValueError, match="pinned revision"):
        build_context_packet(context, repo)


def test_deleted_file_uses_verified_base_source(pinned_evidence):
    import subprocess

    from qa_orchestrator.context_pack import build_context_packet, validate_packet_context
    from qa_orchestrator.contracts import review_diff_hunk_inventory_sha256

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
        update={
            "base_sha": base,
            "head_sha": head,
            "diff_sha256": hashlib.sha256(diff).hexdigest(),
            "diff_hunk_inventory_sha256": review_diff_hunk_inventory_sha256(
                hashlib.sha256(diff).hexdigest(), context.diff_hunks
            ),
        }
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


def test_selection_preview_uses_host_plan_and_preserves_bound_inputs(pinned_evidence, tmp_path, capsys):
    from qa_orchestrator.context_pack import build_context_packet
    from qa_orchestrator.evidence import evidence_main

    repo, _, context, definitions = pinned_evidence
    context = register_evidence(context, definitions, repo)
    packet = build_context_packet(context, repo, optional_profiles={"S1": (ReviewAgent.CODE_REVIEWER,)})
    packet_bytes = packet.model_dump_json().encode()
    context = context.model_copy(update={"evidence_packet_sha256": hashlib.sha256(packet_bytes).hexdigest()})
    context_path, packet_path = tmp_path / "context.json", tmp_path / "packet.json"
    context_path.write_text(context.model_dump_json())
    packet_path.write_bytes(packet_bytes)
    args = ["preview-selection", "--context", str(context_path), "--packet", str(packet_path),
            "--profile", "python_reviewer"]
    assert evidence_main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert "D1" in result["included_refs"]
    assert "S1" in result["omitted_refs"]
    assert "S1" not in result["included_refs"]
    assert result["selected_bytes"] > 0
    with pytest.raises(ValueError, match="must differ"):
        evidence_main(args + ["--output", str(packet_path)])
    assert packet_path.read_bytes() == packet_bytes
    packet_path.write_bytes(packet_bytes + b" ")
    with pytest.raises(ValueError, match="hash mismatch"):
        evidence_main(args)
