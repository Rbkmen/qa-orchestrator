from qa_orchestrator.contracts import ProfileReviewReport, ReviewAgent
from qa_orchestrator.review_cycle import ReviewCycle, ReviewVerdict


async def test_review_cycle_revisits_missing_paths_and_verifies_both_passes(
    bound_review_context, review_finding_candidate,
):
    context = bound_review_context
    first = ProfileReviewReport(profile=ReviewAgent.CODE_REVIEWER, scope="first",
        finding_candidates=(review_finding_candidate,))
    second = ProfileReviewReport(profile=ReviewAgent.CODE_REVIEWER, scope="second",
        finding_candidates=(review_finding_candidate,), reviewed_changed_paths=context.changed_files)
    requests = []
    async def review(request):
        requests.append(request)
        return first if request.phase == "review" else second
    async def verify(request):
        return tuple(ReviewVerdict(candidate_key=item.key, verdict="uncertain",
            rationale="Need the caller.", evidence_refs=("E1",)) for item in request.candidates)
    result = await ReviewCycle(review, verify).run(context, ReviewAgent.CODE_REVIEWER)
    assert [r.phase for r in requests] == ["review", "second_look"]
    assert requests[1].missing_changed_paths == context.changed_files
    assert requests[1].prior_report == first
    assert [v.candidate_key for v in result.verdicts] == ["first:F-01", "second:F-01"]
    assert result.reports == (first, second)


async def test_missing_verifier_verdicts_remain_unverified(bound_review_context, review_finding_candidate):
    report = ProfileReviewReport(profile=ReviewAgent.CODE_REVIEWER, scope="review",
        finding_candidates=(review_finding_candidate,))
    async def review(request):
        return report if request.phase == "review" else report.model_copy(update={"finding_candidates": ()})
    async def verify(request):
        return ()
    result = await ReviewCycle(review, verify).run(bound_review_context, ReviewAgent.CODE_REVIEWER)
    assert result.verdicts[0].verdict == "uncertain"
    assert "missing" in result.verdicts[0].rationale


async def test_failed_verifier_never_discards_candidates(bound_review_context, review_finding_candidate):
    report = ProfileReviewReport(profile=ReviewAgent.CODE_REVIEWER, scope="review",
        finding_candidates=(review_finding_candidate,))
    async def review(request):
        return report if request.phase == "review" else report.model_copy(update={"finding_candidates": ()})
    async def verify(request):
        raise RuntimeError("private details")
    result = await ReviewCycle(review, verify).run(bound_review_context, ReviewAgent.CODE_REVIEWER)
    assert result.reports[0].finding_candidates == (review_finding_candidate,)
    assert result.verdicts[0].verdict == "uncertain"
    assert result.verification_status == "failed"
    assert "private details" not in repr(result)


async def test_unbound_verifier_evidence_is_not_accepted(bound_review_context, review_finding_candidate):
    report = ProfileReviewReport(profile=ReviewAgent.CODE_REVIEWER, scope="review",
        finding_candidates=(review_finding_candidate,))
    async def review(request):
        return report if request.phase == "review" else report.model_copy(update={"finding_candidates": ()})
    async def verify(request):
        return (ReviewVerdict(candidate_key="first:F-01", verdict="confirmed",
            rationale="yes", evidence_refs=("E99",)),)
    result = await ReviewCycle(review, verify).run(bound_review_context, ReviewAgent.CODE_REVIEWER)
    assert result.verification_status == "failed"
    assert result.verdicts[0].verdict == "uncertain"
