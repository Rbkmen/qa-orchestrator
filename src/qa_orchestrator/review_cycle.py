"""Host-driven review, second look and independent candidate verification."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from qa_orchestrator.contracts import (
    ProfileReviewReport,
    ReviewAgent,
    ReviewContext,
    ReviewFindingCandidate,
)
from qa_orchestrator.evidence import validate_context_selection, validate_registered_locations


@dataclass(frozen=True)
class ReviewPassRequest:
    context: ReviewContext
    profile: ReviewAgent
    phase: Literal["review", "second_look"]
    prior_report: ProfileReviewReport | None = None
    missing_changed_paths: tuple[str, ...] = ()
    missing_hunk_indexes: tuple[int, ...] = ()


@dataclass(frozen=True)
class ReviewCycleCandidate:
    key: str
    candidate: ReviewFindingCandidate


@dataclass(frozen=True)
class VerificationRequest:
    context: ReviewContext
    candidates: tuple[ReviewCycleCandidate, ...]


class ReviewVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_key: str = Field(min_length=1, max_length=100)
    verdict: Literal["confirmed", "rejected", "uncertain"]
    rationale: str = Field(min_length=1, max_length=2000)
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=32)


@dataclass(frozen=True)
class ReviewCycleResult:
    reports: tuple[ProfileReviewReport, ...]
    verdicts: tuple[ReviewVerdict, ...]
    second_look_status: Literal["completed", "failed"]
    verification_status: Literal["completed", "partial", "failed", "no_candidates"]


class ReviewCycle:
    """Run host-supplied independent model calls; final decisions remain host-owned."""

    def __init__(
        self,
        review: Callable[[ReviewPassRequest], Awaitable[ProfileReviewReport]],
        verify: Callable[[VerificationRequest], Awaitable[tuple[ReviewVerdict, ...]]],
    ):
        self.review = review
        self.verify = verify

    @staticmethod
    def _validate_report(context: ReviewContext, profile: ReviewAgent, report: ProfileReviewReport):
        if report.profile != profile:
            raise ValueError("review returned the wrong profile")
        if set(report.reviewed_changed_paths) - set(context.changed_files):
            raise ValueError("review reported an unbound changed path")
        if set(report.reviewed_diff_hunk_indexes) - {h.index for h in context.diff_hunks}:
            raise ValueError("review reported an unbound diff hunk")
        for candidate in report.finding_candidates:
            if set(candidate.evidence_refs) - set(context.evidence_refs):
                raise ValueError("candidate cites unbound evidence")
        other_refs = {ref for note in (*report.coverage_gaps, *report.unverified)
                      for ref in note.evidence_refs}
        other_refs.update(item.evidence_ref for item in report.evidence_map)
        if other_refs - set(context.evidence_refs):
            raise ValueError("review cites unbound evidence")
        validate_context_selection(context, report)
        validate_registered_locations(context, report.finding_candidates)

    async def run(self, context: ReviewContext, profile: ReviewAgent) -> ReviewCycleResult:
        first = ProfileReviewReport.model_validate(await self.review(
            ReviewPassRequest(context=context, profile=profile, phase="review")
        ))
        self._validate_report(context, profile, first)
        paths = tuple(path for path in context.changed_files if path not in first.reviewed_changed_paths)
        hunks = tuple(h.index for h in context.diff_hunks if h.index not in first.reviewed_diff_hunk_indexes)
        reports = [first]
        second_status: Literal["completed", "failed"] = "failed"
        try:
            second = ProfileReviewReport.model_validate(await self.review(ReviewPassRequest(
                context=context, profile=profile, phase="second_look", prior_report=first,
                missing_changed_paths=paths, missing_hunk_indexes=hunks,
            )))
            self._validate_report(context, profile, second)
            reports.append(second)
            second_status = "completed"
        except Exception:  # noqa: BLE001 -- a later failure must not erase the first report
            second_status = "failed"
        candidates = tuple(
            ReviewCycleCandidate(f"{label}:{candidate.candidate_id}", candidate)
            for label, report in zip(("first", "second"), reports, strict=False)
            for candidate in report.finding_candidates
        )
        if not candidates:
            return ReviewCycleResult(tuple(reports), (), second_status, "no_candidates")
        try:
            raw = await self.verify(VerificationRequest(context=context, candidates=candidates))
            verdicts = [ReviewVerdict.model_validate(item) for item in raw]
            keys = {item.key for item in candidates}
            if len({v.candidate_key for v in verdicts}) != len(verdicts) or any(v.candidate_key not in keys for v in verdicts):
                raise ValueError("verifier returned duplicate or unknown candidate IDs")
            if any(set(v.evidence_refs) - set(context.evidence_refs) for v in verdicts):
                raise ValueError("verifier cites unbound evidence")
            if any(v.verdict != "uncertain" and not v.evidence_refs for v in verdicts):
                raise ValueError("decisive verifier verdict requires evidence")
        except Exception:  # noqa: BLE001 -- preserve proposed findings, never store provider errors
            return ReviewCycleResult(tuple(reports), tuple(
                ReviewVerdict(candidate_key=item.key, verdict="uncertain", rationale="Verification failed.")
                for item in candidates
            ), second_status, "failed")
        by_key = {v.candidate_key: v for v in verdicts}
        status: Literal["completed", "partial"] = "completed" if len(by_key) == len(candidates) else "partial"
        ordered = tuple(by_key.get(item.key) or ReviewVerdict(candidate_key=item.key,
            verdict="uncertain", rationale="Verifier verdict missing.") for item in candidates)
        return ReviewCycleResult(tuple(reports), ordered, second_status, status)
