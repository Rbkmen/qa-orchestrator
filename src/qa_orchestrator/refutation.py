"""Pinned identities for host-owned checks of candidate counterevidence."""

import hashlib
import json

from qa_orchestrator.contracts import RefutationCheck, ReviewContext, ReviewFindingCandidate


def _digest(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


def make_refutation_check(
    context: ReviewContext,
    candidate: ReviewFindingCandidate,
    *,
    status: str,
    rationale: str,
    evidence_refs: tuple[str, ...],
) -> RefutationCheck:
    check = RefutationCheck(
        context_sha256=_digest(context),
        candidate_sha256=_digest(candidate),
        status=status,
        rationale=rationale,
        evidence_refs=evidence_refs,
    )
    validate_refutation_check(context, candidate, check)
    return check


def validate_refutation_check(
    context: ReviewContext,
    candidate: ReviewFindingCandidate,
    check: RefutationCheck,
) -> None:
    if (
        candidate.failure_condition is None or candidate.flip_condition is None
    ) and check.status != "unverified":
        raise ValueError(
            "legacy candidates without explicit conditions can only have an unverified check"
        )
    if check.context_sha256 != _digest(context):
        raise ValueError("refutation context identity mismatch")
    if check.candidate_sha256 != _digest(candidate):
        raise ValueError("refutation candidate identity mismatch")
    if set(check.evidence_refs) - set(context.evidence_refs):
        raise ValueError("refutation check cites unknown evidence refs")
