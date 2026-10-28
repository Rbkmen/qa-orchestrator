"""Exact, deterministic context selection. Reviewed content never selects its own scope."""

import hashlib
import json
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from qa_orchestrator.contracts import EvidenceRecord, ReviewAgent, ReviewContext
from qa_orchestrator.evidence import _diff, _git, _is_text_source, _source_lines

MAX_CONTEXT_PACKET_BYTES = 2_000_000


def _omitted_source_text(sha256: str) -> str:
    return (
        "[Source content omitted from text context (binary or non-UTF-8); "
        f"sha256={sha256}]"
    )


class ContextFragment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    record: EvidenceRecord
    text: str = Field(max_length=4_000_000)
    required: bool = True
    profiles: tuple[ReviewAgent, ...] = ()
    priority: int = Field(default=0, ge=0, le=100)

    @model_validator(mode="after")
    def validate_content(self) -> "ContextFragment":
        text_sha256 = hashlib.sha256(self.text.encode()).hexdigest()
        omitted_source = (
            self.record.kind == "source"
            and self.text == _omitted_source_text(self.record.sha256)
        )
        if omitted_source and self.record.source_text_available is True:
            raise ValueError("source omission marker conflicts with text availability metadata")
        if (
            self.record.kind == "source"
            and self.record.source_text_available is False
            and not omitted_source
        ):
            raise ValueError("source fragment is marked unavailable but contains text")
        if text_sha256 != self.record.sha256 and not omitted_source:
            raise ValueError("context fragment content hash mismatch")
        if len(set(self.profiles)) != len(self.profiles):
            raise ValueError("context profiles must be unique")
        return self


class ContextPacket(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    format: Literal["qa-orchestrator-context-v1"] = "qa-orchestrator-context-v1"
    fragments: tuple[ContextFragment, ...] = Field(min_length=1, max_length=1000)
    deleted_paths: tuple[str, ...] = Field(default=(), max_length=500)

    @model_validator(mode="after")
    def validate_refs(self) -> "ContextPacket":
        refs = [f.record.evidence_ref for f in self.fragments]
        if len(set(refs)) != len(refs):
            raise ValueError("context refs must be unique")
        if len(set(self.deleted_paths)) != len(self.deleted_paths):
            raise ValueError("deleted paths must be unique")
        return self


def _packet_envelope_size(deleted_paths: tuple[str, ...]) -> int:
    probe = ContextFragment(
        record=EvidenceRecord(
            evidence_ref="size-probe",
            kind="other",
            locator="host:size-probe",
            sha256=hashlib.sha256(b"").hexdigest(),
        ),
        text="",
    )
    packet = ContextPacket(fragments=(probe,), deleted_paths=deleted_paths)
    return len(packet.model_dump_json().encode("utf-8")) - len(
        probe.model_dump_json().encode("utf-8")
    )


@dataclass(frozen=True)
class PackedContext:
    text: str
    sha256: str
    included_refs: tuple[str, ...]
    omitted_refs: tuple[str, ...]
    budget_omitted_refs: tuple[str, ...]


def select_context(
    packet: ContextPacket,
    profile: ReviewAgent,
    *,
    max_bytes: int,
    profile_override_refs: tuple[str, ...] = (),
    required_refs: tuple[str, ...] = (),
) -> PackedContext:
    if not 1 <= max_bytes <= 2_000_000:
        raise ValueError("context byte budget outside supported bounds")
    profile = ReviewAgent(profile)
    override_refs = set(profile_override_refs)
    required_ref_set = set(required_refs)
    packet_refs = {fragment.record.evidence_ref for fragment in packet.fragments}
    if override_refs - packet_refs:
        raise ValueError("profile override refs must belong to the context packet")
    if required_ref_set - packet_refs:
        raise ValueError("required refs must belong to the context packet")
    groups = {}
    omitted, budget_omitted = [], []
    for fragment in sorted(packet.fragments, key=lambda f: f.record.evidence_ref):
        if (
            not fragment.required
            and fragment.profiles
            and profile not in fragment.profiles
            and fragment.record.evidence_ref not in override_refs
            and fragment.record.evidence_ref not in required_ref_set
        ):
            omitted.append(fragment.record.evidence_ref)
            continue
        identity = fragment.record.model_dump(exclude={"evidence_ref"})
        key = json.dumps(identity, sort_keys=True)
        groups.setdefault(key, []).append(fragment)
    ordered = sorted(
        groups.values(),
        key=lambda fs: (
            not any(
                f.required
                or f.record.evidence_ref in override_refs
                or f.record.evidence_ref in required_ref_set
                for f in fs
            ),
            -max(f.priority for f in fs),
            fs[0].record.evidence_ref,
        ),
    )
    blocks, included, size = [], [], 0
    for fragments in ordered:
        # Aliases share bytes only when revision, coordinates, locator and hash all agree.
        metadata = [f.record.model_dump(mode="json", exclude_none=True) for f in fragments]
        block = json.dumps(metadata, sort_keys=True, ensure_ascii=False) + "\n" + fragments[0].text
        block += "\nEND EVIDENCE FRAGMENT\n"
        cost = len(block.encode())
        refs = [f.record.evidence_ref for f in fragments]
        if size + cost > max_bytes:
            if any(
                f.required or f.record.evidence_ref in required_ref_set
                for f in fragments
            ):
                raise ValueError("mandatory context exceeds budget; split the review explicitly")
            omitted.extend(refs)
            budget_omitted.extend(refs)
            continue
        blocks.append(block)
        included.extend(refs)
        size += cost
    text = "".join(blocks)
    return PackedContext(
        text,
        hashlib.sha256(text.encode()).hexdigest(),
        tuple(included),
        tuple(sorted(omitted)),
        tuple(sorted(budget_omitted)),
    )


class PinnedSourceCache:
    """Bounded in-memory blob cache. No worktree reads or persistent review memory."""

    def __init__(self, *, max_bytes: int = 16_000_000):
        if not 1 <= max_bytes <= 64_000_000:
            raise ValueError("source cache size outside supported bounds")
        self.max_bytes = max_bytes
        self.hits = 0
        self._blobs: OrderedDict[str, bytes] = OrderedDict()
        self._size = 0

    def read(self, repository: Path, entry: EvidenceRecord) -> bytes:
        blob = (
            _git(repository, "rev-parse", "--verify", f"{entry.revision}:{entry.path}")
            .decode()
            .strip()
        )
        if blob in self._blobs:
            self.hits += 1
            data = self._blobs.pop(blob)
            self._blobs[blob] = data
        else:
            if int(_git(repository, "cat-file", "-s", blob)) > 4_000_000:
                raise ValueError("source blob exceeds supported size")
            data = _git(repository, "cat-file", "blob", blob)
            if len(data) > 4_000_000:
                raise ValueError("source blob exceeds supported size")
            if len(data) <= self.max_bytes:
                while self._blobs and (
                    self._size + len(data) > self.max_bytes or len(self._blobs) >= 128
                ):
                    _, old = self._blobs.popitem(last=False)
                    self._size -= len(old)
                self._blobs[blob] = data
                self._size += len(data)
        if entry.start_line is None:
            return data
        lines = _source_lines(data)
        end = entry.end_line or entry.start_line
        if end > len(lines):
            raise ValueError("source range outside pinned blob")
        return b"".join(lines[entry.start_line - 1 : end])


def build_context_packet(
    context: ReviewContext,
    repository: Path,
    *,
    cache: PinnedSourceCache | None = None,
    optional_profiles: dict[str, tuple[ReviewAgent, ...]] | None = None,
    priorities: dict[str, int] | None = None,
    external_content: dict[str, str] | None = None,
) -> ContextPacket:
    if not context.evidence_registry:
        raise ValueError("context packing requires a complete evidence registry")
    deleted = tuple(
        sorted(
            set(
                _git(
                    repository,
                    "diff",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--diff-filter=D",
                    "--name-only",
                    "-z",
                    context.base_sha,
                    context.head_sha,
                    "--",
                )
                .decode("utf-8")
                .rstrip("\x00")
                .split("\x00")
            )
            - {""}
        )
    )
    deleted = tuple(path for path in deleted if path in context.changed_files)
    _validate_mandatory_presence(context, deleted)
    packet_size = _packet_envelope_size(deleted)
    cache = cache or PinnedSourceCache()
    optional_profiles, priorities, external_content = (
        optional_profiles or {},
        priorities or {},
        external_content or {},
    )
    refs = set(context.evidence_refs)
    if (set(optional_profiles) | set(priorities) | set(external_content)) - refs:
        raise ValueError("context plan cites unknown evidence refs")
    diff = _diff(context, repository)
    fragments = []
    for entry in context.evidence_registry:
        if entry.evidence_ref in optional_profiles and entry.kind in {"diff", "requirement", "claim"}:
            raise ValueError("diff, requirements, and MR claims must remain mandatory")
        if entry.kind == "source":
            source = cache.read(repository, entry)
            if hashlib.sha256(source).hexdigest() != entry.sha256:
                raise ValueError(f"source evidence content hash mismatch: {entry.evidence_ref}")
            source_text_available = _is_text_source(source)
            if (
                entry.source_text_available is not None
                and source_text_available != entry.source_text_available
            ):
                raise ValueError(
                    f"source text availability metadata mismatch: {entry.evidence_ref}"
                )
            text = (
                source.decode("utf-8")
                if source_text_available
                else _omitted_source_text(entry.sha256)
            )
        elif entry.kind == "diff":
            text = diff.decode("utf-8")
        else:
            if entry.evidence_ref not in external_content:
                raise ValueError("external evidence requires explicit host content")
            text = external_content[entry.evidence_ref]
        if packet_size + len(text.encode("utf-8")) > MAX_CONTEXT_PACKET_BYTES:
            raise ValueError("context packet exceeds executor input size")
        fragment = ContextFragment(
            record=entry,
            text=text,
            required=entry.evidence_ref not in optional_profiles,
            profiles=optional_profiles.get(entry.evidence_ref, ()),
            priority=priorities.get(entry.evidence_ref, 0),
        )
        fragment_size = len(fragment.model_dump_json().encode("utf-8"))
        separator_size = 1 if fragments else 0
        if packet_size + separator_size + fragment_size > MAX_CONTEXT_PACKET_BYTES:
            raise ValueError("context packet exceeds executor input size")
        packet_size += separator_size + fragment_size
        fragments.append(fragment)
    return ContextPacket(
        fragments=tuple(sorted(fragments, key=lambda f: f.record.evidence_ref)),
        deleted_paths=deleted,
    )


def validate_packet_context(packet: ContextPacket, context: ReviewContext) -> None:
    _validate_mandatory_presence(context, packet.deleted_paths)
    registry = {r.evidence_ref: r for r in context.evidence_registry}
    if {f.record.evidence_ref for f in packet.fragments} != set(context.evidence_refs):
        raise ValueError("context packet must cover every bound evidence ref")
    for fragment in packet.fragments:
        if fragment.record != registry.get(fragment.record.evidence_ref):
            raise ValueError("context packet metadata differs from the bound registry")
        if not fragment.required and fragment.record.kind in {"diff", "requirement", "claim"}:
            raise ValueError("mandatory diff/requirements/MR claims cannot be excluded")


def _validate_mandatory_presence(context: ReviewContext, deleted_paths: tuple[str, ...]) -> None:
    if not any(r.kind == "diff" for r in context.evidence_registry):
        raise ValueError("mandatory diff evidence is absent")
    if set(deleted_paths) - set(context.changed_files):
        raise ValueError("deleted paths must belong to the bound changed files")
    expected_revisions = {
        path: context.base_sha if path in deleted_paths else context.head_sha
        for path in context.changed_files
    }
    source_revisions_by_path: dict[str, set[str]] = {}
    for entry in context.evidence_registry:
        if entry.kind != "source" or entry.path not in expected_revisions:
            continue
        expected_revision = expected_revisions[entry.path]
        allowed_revisions = {expected_revision}
        if expected_revision == context.head_sha:
            allowed_revisions.add(context.base_sha)
        if entry.revision not in allowed_revisions:
            raise ValueError("changed source evidence must match the pinned revision")
        source_revisions_by_path.setdefault(entry.path, set()).add(entry.revision)
    if any(
        expected_revisions[path] not in revisions
        for path, revisions in source_revisions_by_path.items()
    ):
        raise ValueError("changed source evidence must match the pinned revision")
