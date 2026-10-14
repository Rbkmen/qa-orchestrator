import pytest
from pydantic import ValidationError

from qa_orchestrator.contracts import FinalReviewReport, ReviewAgent, ReviewCandidateDecision
from qa_orchestrator.orchestration import DeepReviewSignals, OrchestrationError, QaOrchestrator


@pytest.fixture
def reviewed_candidate(
    request,
    bound_review_context,
    review_finding_candidate,
    make_profile_review_report,
    make_final_review_report,
):
    if getattr(request, "param", False):
        review_finding_candidate = review_finding_candidate.model_copy(update={
            "failure_condition": "Repeated request reaches the side effect twice",
            "flip_condition": "A deduplication guard prevents the second side effect",
        })
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review", bound_review_context)
    orchestrator.advance(
        run_id=session.run_id,
        completed_step="triage",
        status="completed",
        selected_profile=ReviewAgent.CODE_REVIEWER,
    )
    orchestrator.advance(
        run_id=session.run_id,
        completed_step="primary_review",
        status="completed",
        completed_profile=ReviewAgent.CODE_REVIEWER,
        profile_reports=[
            make_profile_review_report(
                ReviewAgent.CODE_REVIEWER, findings=(review_finding_candidate,)
            )
        ],
        risk_signals=DeepReviewSignals(),
    )
    orchestrator.advance(run_id=session.run_id, completed_step="synthesis", status="completed")
    report = make_final_review_report(
        (ReviewAgent.CODE_REVIEWER,),
        findings=((review_finding_candidate, (ReviewAgent.CODE_REVIEWER,)),),
    )
    return orchestrator, session.run_id, report


@pytest.mark.parametrize("reviewed_candidate", [True], indirect=True)
def test_finalization_requires_and_checks_pinned_refutation(reviewed_candidate):
    from qa_orchestrator.refutation import make_refutation_check

    orchestrator, run_id, report = reviewed_candidate
    with pytest.raises(OrchestrationError, match="refutation check"):
        orchestrator.finish(run_id=run_id, outcome="completed", final_review_report=report)
    session = orchestrator.get(run_id)
    candidate = session.profile_reports[0].finding_candidates[0]
    check = make_refutation_check(session.review_context, candidate, status="not_refuted",
                                 rationale="No preceding guard found", evidence_refs=("E1",))
    payload = report.model_dump()
    payload["candidate_decisions"][0]["refutation"] = check.model_dump()
    stale = report.model_dump()
    stale["candidate_decisions"][0]["refutation"] = {
        **check.model_dump(), "candidate_sha256": "f"*64}
    with pytest.raises(OrchestrationError, match="candidate identity"):
        orchestrator.finish(run_id=run_id, outcome="completed", final_review_report=stale)
    finished = orchestrator.finish(run_id=run_id, outcome="completed", final_review_report=payload)
    assert finished.final_review_report.candidate_decisions[0].refutation == check


def test_new_final_reports_cannot_silently_drop_candidate_decisions(reviewed_candidate):
    orchestrator, run_id, report = reviewed_candidate
    payload = report.model_dump()
    payload["candidate_decisions"] = []
    with pytest.raises(OrchestrationError, match="exactly one decision"):
        orchestrator.finish(run_id=run_id, outcome="completed", final_review_report=payload)
    assert orchestrator.get(run_id).final_review_report is None


@pytest.mark.parametrize("field", ("impact", "affected_area", "severity", "source_locations"))
def test_confirmed_findings_require_a_usable_host_verdict(reviewed_candidate, field):
    orchestrator, run_id, report = reviewed_candidate
    payload = report.model_dump()
    payload["finding_candidates"][0][field] = [] if field == "source_locations" else None
    with pytest.raises(OrchestrationError, match="confirmed findings require"):
        orchestrator.finish(run_id=run_id, outcome="completed", final_review_report=payload)


def test_rejected_candidate_is_retained_without_a_final_finding(reviewed_candidate):
    orchestrator, run_id, report = reviewed_candidate
    payload = report.model_dump()
    payload["finding_candidates"] = []
    decision = payload["candidate_decisions"][0]
    decision.update(
        verdict="rejected",
        final_finding_id=None,
        rationale="The supported path already validates this branch.",
    )
    result = orchestrator.finish(run_id=run_id, outcome="completed", final_review_report=payload)
    assert result.final_review_report.finding_candidates == ()
    assert result.final_review_report.candidate_decisions[0].verdict == "rejected"


def test_candidate_refutation_conditions_require_a_complete_nonblank_pair(review_finding_candidate):
    from qa_orchestrator.contracts import ReviewFindingCandidate

    payload = review_finding_candidate.model_dump()
    with pytest.raises(ValidationError, match="both failure_condition and flip_condition"):
        ReviewFindingCandidate.model_validate({**payload, "flip_condition": "Prove deduplication"})
    with pytest.raises(ValidationError, match="blank"):
        ReviewFindingCandidate.model_validate({
            **payload, "failure_condition": "Repeat request", "flip_condition": "   "})


def test_refutation_check_is_bound_to_context_and_candidate(review_finding_candidate, bound_review_context):
    from qa_orchestrator.refutation import make_refutation_check, validate_refutation_check

    candidate = review_finding_candidate.model_copy(update={
        "failure_condition": "Same request can reach the side effect twice",
        "flip_condition": "A source-backed deduplication guard precedes the side effect",
    })
    check = make_refutation_check(bound_review_context, candidate, status="not_refuted",
                                 rationale="No guard on the inspected path", evidence_refs=("E1",))
    validate_refutation_check(bound_review_context, candidate, check)
    with pytest.raises(ValueError, match="candidate identity"):
        validate_refutation_check(bound_review_context, candidate.model_copy(update={
            "flip_condition": "A different counterexample"}), check)
    with pytest.raises(ValueError, match="context identity"):
        validate_refutation_check(bound_review_context.model_copy(update={"head_sha": "c"*40}),
                                  candidate, check)


def test_refuted_candidate_cannot_be_confirmed():
    from qa_orchestrator.contracts import RefutationCheck

    check = RefutationCheck(context_sha256="a"*64, candidate_sha256="b"*64,
                            status="refuted", rationale="Guard exists", evidence_refs=("E1",))
    with pytest.raises(ValidationError, match="refutation"):
        ReviewCandidateDecision(source={"profile": "code_reviewer", "candidate_id": "F-01"},
                                verdict="confirmed", rationale="Defect", evidence_refs=("E1",),
                                final_finding_id="F-01", refutation=check)


def test_a_duplicate_cannot_be_the_only_basis_for_confirmation(reviewed_candidate):
    orchestrator, run_id, report = reviewed_candidate
    payload = report.model_dump()
    payload["candidate_decisions"][0]["verdict"] = "duplicate"
    with pytest.raises(OrchestrationError, match="confirmed and duplicate decisions"):
        orchestrator.finish(run_id=run_id, outcome="completed", final_review_report=payload)


def test_decision_sources_and_final_links_are_unique_and_explicit(reviewed_candidate):
    _, _, report = reviewed_candidate
    payload = report.model_dump()
    payload["candidate_decisions"] *= 2
    with pytest.raises(ValidationError, match="unique sources"):
        FinalReviewReport.model_validate(payload)
    with pytest.raises(ValidationError, match="only confirmed and duplicate"):
        ReviewCandidateDecision(
            source={"profile": "code_reviewer", "candidate_id": "F-01"},
            verdict="unverified",
            final_finding_id="F-01",
            rationale="Runtime is unavailable.",
            evidence_refs=("E1",),
        )


@pytest.mark.parametrize("outcome", ("partial", "blocked"))
def test_early_stop_can_attach_its_first_final_report(
    outcome,
    bound_review_context,
    review_finding_candidate,
    make_profile_review_report,
    make_final_review_report,
):
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review", bound_review_context)
    orchestrator.advance(
        run_id=session.run_id,
        completed_step="triage",
        status="completed",
        selected_bundle="autotest",
    )
    orchestrator.advance(
        run_id=session.run_id,
        completed_step="primary_review",
        status="completed",
        completed_profile="code_reviewer",
        profile_reports=[
            make_profile_review_report(
                ReviewAgent.CODE_REVIEWER, findings=(review_finding_candidate,)
            )
        ],
    )
    stopped = orchestrator.advance(
        run_id=session.run_id, completed_step="primary_review", status=outcome
    )
    final = make_final_review_report(
        (ReviewAgent.CODE_REVIEWER,),
        findings=((review_finding_candidate, (ReviewAgent.CODE_REVIEWER,)),),
    )
    finished = orchestrator.finish(
        run_id=session.run_id,
        outcome=outcome,
        final_review_report=final,
        expected_revision=stopped.revision,
    )
    assert finished.final_review_report == final
    assert (
        orchestrator.finish(run_id=session.run_id, outcome=outcome, final_review_report=final)
        == finished
    )
