import pytest
from pydantic import ValidationError

from qa_orchestrator.contracts import FinalReviewReport, ReviewAgent, ReviewCandidateDecision
from qa_orchestrator.orchestration import DeepReviewSignals, OrchestrationError, QaOrchestrator


@pytest.fixture
def reviewed_candidate(
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
