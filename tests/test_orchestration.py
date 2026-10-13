from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from qa_orchestrator.contracts import ReviewAgent, ReviewBundle, ReviewFindingCandidate, ReviewNote
from qa_orchestrator.orchestration import (
    DeepReviewRule,
    DeepReviewSignals,
    OrchestrationError,
    OrchestrationReason,
    OrchestrationStatus,
    OrchestrationStep,
    QaOrchestrator,
)
from qa_orchestrator.review_profiles import (
    REVIEW_BUNDLES,
    bundle_execution_groups,
)


def _evidenced_signals(**signals):
    return DeepReviewSignals(
        **signals,
        rationale="The reviewed evidence supports this escalation signal.",
        evidence_refs=("E1",),
    )


def test_delete_frees_capacity_and_concurrent_repeats_are_idempotent():
    orchestrator = QaOrchestrator(ttl_seconds=60, max_sessions=1)
    started = orchestrator.start("ordinary_review")
    with pytest.raises(OrchestrationError, match="session limit"):
        orchestrator.start("other")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(orchestrator.delete, [started.run_id] * 8))
    assert all(result == results[0] for result in results)
    assert orchestrator.list_sessions().sessions == []
    assert orchestrator.start("other").run_id != started.run_id


def test_list_includes_retained_terminal_sessions_and_returns_independent_results():
    orchestrator = QaOrchestrator(ttl_seconds=60, max_sessions=2)
    assert orchestrator.list_sessions().sessions == []
    started = orchestrator.start("ordinary_review")
    orchestrator.advance(run_id=started.run_id, completed_step="triage", status="partial")
    orchestrator.finish(run_id=started.run_id, outcome="partial")
    active = orchestrator.start("widget_review")

    listed = orchestrator.list_sessions()
    assert {entry.run_id: entry.status.value for entry in listed.sessions} == {
        started.run_id: "partial",
        active.run_id: "active",
    }
    listed.sessions.clear()
    assert len(orchestrator.list_sessions().sessions) == 2

    newest = orchestrator.start("qa_planning")
    assert {entry.run_id for entry in orchestrator.list_sessions().sessions} == {
        active.run_id,
        newest.run_id,
    }


def test_normal_flow_skips_sol():
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review")

    assert session.current_step is OrchestrationStep.TRIAGE
    assert session.model_policy.model.value == "gpt-6-luna"
    assert session.model_policy.reasoning == "max"
    assert session.next_action == "Host performs triage and submits the selected review bundle or profile."

    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.TRIAGE,
        status="completed",
        selected_profile="code_reviewer",
    )
    assert session.current_step is OrchestrationStep.PRIMARY_REVIEW
    assert session.model_policy.reasoning == "medium"
    assert session.next_action == "Host performs primary review with the selected ordered profiles."

    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.PRIMARY_REVIEW,
        status="completed",
        completed_profile=ReviewAgent.CODE_REVIEWER,
        risk_signals=DeepReviewSignals(),
    )
    assert session.current_step is OrchestrationStep.SYNTHESIS
    assert session.next_action == "Host performs final synthesis and validates the QA result."

    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.SYNTHESIS,
        status="completed",
    )
    assert session.status is OrchestrationStatus.AWAITING_HOST_OUTCOME
    assert session.current_step is OrchestrationStep.AWAITING_HOST_OUTCOME
    assert session.model_policy is None
    assert session.next_action == (
        "Host submits the final QA outcome with finish_qa_orchestration."
    )


def test_review_bound_flow_validates_and_retains_structured_reports(
    bound_review_context,
    review_finding_candidate,
    make_profile_review_report,
    make_final_review_report,
):
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    started = orchestrator.start("ordinary_review", bound_review_context)
    assert started.recommended_profiles == [ReviewAgent.PYTHON_REVIEWER]
    primary = orchestrator.advance(
        run_id=started.run_id,
        completed_step=OrchestrationStep.TRIAGE,
        status="completed",
        selected_profile=ReviewAgent.CODE_REVIEWER,
    )

    with pytest.raises(OrchestrationError, match="profile_reports are required"):
        orchestrator.advance(
            run_id=started.run_id,
            completed_step=primary.current_step,
            status="completed",
            completed_profile=ReviewAgent.CODE_REVIEWER,
            risk_signals=DeepReviewSignals(),
        )

    unknown_reference_candidate = ReviewFindingCandidate(
        **{
            **review_finding_candidate.model_dump(),
            "evidence_refs": ("E9",),
        }
    )
    with pytest.raises(OrchestrationError, match="unknown evidence refs"):
        orchestrator.advance(
            run_id=started.run_id,
            completed_step=primary.current_step,
            status="completed",
            completed_profile=ReviewAgent.CODE_REVIEWER,
            profile_reports=[
                make_profile_review_report(
                    ReviewAgent.CODE_REVIEWER,
                    findings=(unknown_reference_candidate,),
                )
            ],
            risk_signals=DeepReviewSignals(),
        )

    with pytest.raises(OrchestrationError, match="risk signals cite unknown evidence refs"):
        orchestrator.advance(
            run_id=started.run_id,
            completed_step=primary.current_step,
            status="completed",
            completed_profile=ReviewAgent.CODE_REVIEWER,
            profile_reports=[make_profile_review_report(ReviewAgent.CODE_REVIEWER)],
            risk_signals=DeepReviewSignals(
                high_risk_domain=True,
                rationale="An access-control claim remains uncertain.",
                evidence_refs=("E9",),
            ),
        )

    reviewed = orchestrator.advance(
        run_id=started.run_id,
        completed_step=primary.current_step,
        status="completed",
        completed_profile=ReviewAgent.CODE_REVIEWER,
        profile_reports=[
            make_profile_review_report(
                ReviewAgent.CODE_REVIEWER,
                findings=(review_finding_candidate,),
            )
        ],
        risk_signals=DeepReviewSignals(),
    )
    awaiting_outcome = orchestrator.advance(
        run_id=started.run_id,
        completed_step=reviewed.current_step,
        status="completed",
    )
    assert awaiting_outcome.profile_reports[0].finding_candidates[0] == review_finding_candidate

    with pytest.raises(OrchestrationError, match="require a final review report"):
        orchestrator.finish(run_id=started.run_id, outcome="completed")

    final_report = make_final_review_report(
        (ReviewAgent.CODE_REVIEWER,),
        findings=((review_finding_candidate, (ReviewAgent.CODE_REVIEWER,)),),
    )
    unreported_candidate = ReviewFindingCandidate(
        **{**review_finding_candidate.model_dump(), "candidate_id": "F-99"}
    )
    unreported_final_report = make_final_review_report(
        (ReviewAgent.CODE_REVIEWER,),
        findings=((unreported_candidate, (ReviewAgent.CODE_REVIEWER,)),),
    )
    with pytest.raises(OrchestrationError, match="source candidates do not match profile reports"):
        orchestrator.finish(
            run_id=started.run_id,
            outcome="completed",
            final_review_report=unreported_final_report,
        )

    unsupported_evidence_candidate = ReviewFindingCandidate(
        **{**review_finding_candidate.model_dump(), "evidence_refs": ("E2",)}
    )
    unsupported_evidence_report = make_final_review_report(
        (ReviewAgent.CODE_REVIEWER,),
        findings=((unsupported_evidence_candidate, (ReviewAgent.CODE_REVIEWER,)),),
    )
    with pytest.raises(OrchestrationError, match="evidence absent from its source candidates"):
        orchestrator.finish(
            run_id=started.run_id,
            outcome="completed",
            final_review_report=unsupported_evidence_report,
        )

    unsupported_location_candidate = ReviewFindingCandidate(
        **{
            **review_finding_candidate.model_dump(),
            "source_locations": ({"path": "src/service.py", "start_line": 43, "end_line": None},),
        }
    )
    unsupported_location_report = make_final_review_report(
        (ReviewAgent.CODE_REVIEWER,),
        findings=((unsupported_location_candidate, (ReviewAgent.CODE_REVIEWER,)),),
    )
    with pytest.raises(OrchestrationError, match="locations are absent from its source candidates"):
        orchestrator.finish(
            run_id=started.run_id,
            outcome="completed",
            final_review_report=unsupported_location_report,
        )

    unlinked_final_report = final_report.model_dump()
    unlinked_final_report["finding_candidates"][0].pop("source_candidates")
    with pytest.raises(OrchestrationError, match="final findings require source candidates"):
        orchestrator.finish(
            run_id=started.run_id,
            outcome="completed",
            final_review_report=unlinked_final_report,
        )

    finished = orchestrator.finish(
        run_id=started.run_id,
        outcome="completed",
        final_review_report=final_report,
    )

    assert finished.final_review_report == final_report
    assert orchestrator.get(started.run_id).final_review_report == final_report
    assert orchestrator.list_sessions().sessions[0].has_review_artifacts


def test_review_bound_fast_path_cannot_skip_reported_coverage_gaps(
    bound_review_context,
    make_profile_review_report,
):
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    started = orchestrator.start("ordinary_review", bound_review_context)
    primary = orchestrator.advance(
        run_id=started.run_id,
        completed_step=OrchestrationStep.TRIAGE,
        status="completed",
        selected_profile=ReviewAgent.CODE_REVIEWER,
    )

    with pytest.raises(OrchestrationError, match="coverage gaps"):
        orchestrator.advance(
            run_id=started.run_id,
            completed_step=primary.current_step,
            status="completed",
            completed_profile=ReviewAgent.CODE_REVIEWER,
            profile_reports=[
                make_profile_review_report(
                    ReviewAgent.CODE_REVIEWER,
                    coverage_gaps=(ReviewNote(text="No test covers the new branch.", evidence_refs=("E2",)),),
                )
            ],
            risk_signals=DeepReviewSignals(),
            skip_synthesis=True,
        )


def test_finish_rejects_review_artifacts_over_the_session_size_limit(
    bound_review_context,
    make_profile_review_report,
    make_final_review_report,
):
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review", bound_review_context)
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.TRIAGE,
        status="completed",
        selected_bundle=ReviewBundle.WIDGET,
    )
    groups = bundle_execution_groups(ReviewBundle.WIDGET)
    large_profile_gaps = tuple(ReviewNote(text="x" * 2000) for _ in range(25))

    for index, group in enumerate(groups):
        session = orchestrator.advance(
            run_id=session.run_id,
            completed_step=session.current_step,
            status="completed",
            completed_profiles=list(group),
            profile_reports=[
                make_profile_review_report(
                    profile,
                    coverage_gaps=large_profile_gaps,
                )
                for profile in group
            ],
            risk_signals=DeepReviewSignals() if index == len(groups) - 1 else None,
        )

    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=session.current_step,
        status="completed",
    )
    large_final_gaps = tuple(ReviewNote(text="y" * 2000) for _ in range(30))
    final_report = make_final_review_report(
        tuple(session.completed_profiles),
        coverage_gaps=large_final_gaps,
    )

    with pytest.raises(OrchestrationError, match="session review artifacts exceed"):
        orchestrator.finish(
            run_id=session.run_id,
            outcome="completed",
            final_review_report=final_report,
        )


def test_clean_single_profile_can_skip_standalone_synthesis():
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review")
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.TRIAGE,
        status="completed",
        selected_profile="code_reviewer",
    )

    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.PRIMARY_REVIEW,
        status="completed",
        completed_profile=ReviewAgent.CODE_REVIEWER,
        risk_signals=DeepReviewSignals(),
        skip_synthesis=True,
    )

    assert session.current_step is OrchestrationStep.AWAITING_HOST_OUTCOME
    assert session.status is OrchestrationStatus.AWAITING_HOST_OUTCOME
    assert session.model_policy is None
    assert session.completed_profiles == [ReviewAgent.CODE_REVIEWER]
    assert session.next_action == (
        "Host submits the final QA outcome with finish_qa_orchestration."
    )
    assert orchestrator.finish(run_id=session.run_id, outcome="completed").status is (
        OrchestrationStatus.COMPLETED
    )


def test_bundle_cannot_skip_synthesis():
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review")
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.TRIAGE,
        status="completed",
        selected_bundle=ReviewBundle.REQUIREMENTS,
    )

    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.PRIMARY_REVIEW,
        status="completed",
        completed_profile=ReviewAgent.CODE_EXPLORER,
    )
    with pytest.raises(OrchestrationError, match="only for a clean single-profile review"):
        orchestrator.advance(
            run_id=session.run_id,
            completed_step=OrchestrationStep.PRIMARY_REVIEW,
            status="completed",
            completed_profile=ReviewAgent.CODE_REVIEWER,
            risk_signals=DeepReviewSignals(),
            skip_synthesis=True,
        )

    current = orchestrator.get(session.run_id)
    assert current.current_step is OrchestrationStep.PRIMARY_REVIEW
    assert current.current_profile is ReviewAgent.CODE_REVIEWER
    assert current.completed_profiles == [ReviewAgent.CODE_EXPLORER]


def test_single_profile_with_any_risk_signal_cannot_skip_synthesis():
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review")
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.TRIAGE,
        status="completed",
        selected_profile="code_reviewer",
    )

    with pytest.raises(OrchestrationError, match="only for a clean single-profile review"):
        orchestrator.advance(
            run_id=session.run_id,
            completed_step=OrchestrationStep.PRIMARY_REVIEW,
            status="completed",
            completed_profile=ReviewAgent.CODE_REVIEWER,
            risk_signals=_evidenced_signals(high_risk_domain=True),
            skip_synthesis=True,
        )

    current = orchestrator.get(session.run_id)
    assert current.current_step is OrchestrationStep.PRIMARY_REVIEW
    assert current.completed_profiles == []


def test_final_host_outcome_completes_session_and_is_idempotent():
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review")
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.TRIAGE,
        status="completed",
        selected_profile="code_reviewer",
    )
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.PRIMARY_REVIEW,
        status="completed",
        completed_profile=ReviewAgent.CODE_REVIEWER,
        risk_signals=DeepReviewSignals(),
    )
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.SYNTHESIS,
        status="completed",
    )

    completed = orchestrator.finish(run_id=session.run_id, outcome="completed")

    assert completed.status is OrchestrationStatus.COMPLETED
    assert completed.current_step is OrchestrationStep.AWAITING_HOST_OUTCOME

    repeated = orchestrator.finish(run_id=session.run_id, outcome="completed")
    assert repeated.status is OrchestrationStatus.COMPLETED


def test_final_host_outcome_can_stop_after_synthesis():
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review")
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.TRIAGE,
        status="completed",
        selected_profile="code_reviewer",
    )
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.PRIMARY_REVIEW,
        status="completed",
        completed_profile=ReviewAgent.CODE_REVIEWER,
        risk_signals=DeepReviewSignals(),
    )
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.SYNTHESIS,
        status="completed",
    )

    partial = orchestrator.finish(run_id=session.run_id, outcome="partial")

    assert partial.status is OrchestrationStatus.PARTIAL


def test_terminal_session_does_not_consume_active_session_limit():
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=1)
    session = orchestrator.start("ordinary_review")

    blocked = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.TRIAGE,
        status="blocked",
    )

    assert blocked.status is OrchestrationStatus.BLOCKED
    replacement = orchestrator.start("ordinary_review")

    assert replacement.status is OrchestrationStatus.ACTIVE
    with pytest.raises(OrchestrationError, match="unknown run_id"):
        orchestrator.get(session.run_id)


def test_concurrent_starts_respect_active_session_limit():
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=1)

    def attempt_start() -> bool:
        try:
            orchestrator.start("ordinary_review")
            return True
        except OrchestrationError:
            return False

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: attempt_start(), range(8)))

    assert sum(results) == 1


def test_deep_flow_returns_to_configured_synthesis():
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review")
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.TRIAGE,
        status="completed",
        selected_profile="security_reviewer",
    )
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.PRIMARY_REVIEW,
        status="completed",
        completed_profile=ReviewAgent.SECURITY_REVIEWER,
        risk_signals=_evidenced_signals(
            high_risk_domain=True,
            evidence_uncertain=True,
        ),
    )

    assert session.current_step is OrchestrationStep.DEEP_REVIEW
    assert session.model_policy.model.value == "gpt-6-sol"
    assert session.model_policy.reasoning == "high"
    assert session.next_action == (
        "Host performs optional read-only deep analysis for the fixed escalation reason."
    )

    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.DEEP_REVIEW,
        status="completed",
    )
    assert session.current_step is OrchestrationStep.SYNTHESIS
    assert session.next_action == "Host performs final synthesis and validates the QA result."


def test_structured_signals_select_sol_and_expose_the_reason():
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review")
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.TRIAGE,
        status="completed",
        selected_profile="code_reviewer",
    )

    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.PRIMARY_REVIEW,
        status="completed",
        completed_profile=ReviewAgent.CODE_REVIEWER,
        risk_signals=_evidenced_signals(
            high_risk_domain=True,
            evidence_uncertain=True,
        ),
    )

    assert session.current_step is OrchestrationStep.DEEP_REVIEW
    assert session.deep_reason_code is OrchestrationReason.HIGH_RISK_DOMAIN
    assert session.deep_assessment is not None
    assert session.deep_assessment.should_escalate is True
    assert session.deep_assessment.triggered_rules == (
        DeepReviewRule.HIGH_RISK_WITH_UNCERTAINTY,
    )
    assert session.risk_signals is not None
    assert session.risk_signals.rationale
    assert session.risk_signals.evidence_refs == ("E1",)


def test_final_primary_profile_requires_risk_signals():
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review")
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.TRIAGE,
        status="completed",
        selected_profile="code_reviewer",
    )

    with pytest.raises(OrchestrationError, match="risk_signals are required"):
        orchestrator.advance(
            run_id=session.run_id,
            completed_step=OrchestrationStep.PRIMARY_REVIEW,
            status="completed",
            completed_profile=ReviewAgent.CODE_REVIEWER,
        )

    assert orchestrator.get(session.run_id).completed_profiles == []


def test_structured_signals_can_explicitly_skip_sol():
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review")
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.TRIAGE,
        status="completed",
        selected_profile="code_reviewer",
    )

    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.PRIMARY_REVIEW,
        status="completed",
        completed_profile=ReviewAgent.CODE_REVIEWER,
        risk_signals=_evidenced_signals(cross_system_scope=True),
    )

    assert session.current_step is OrchestrationStep.SYNTHESIS
    assert session.deep_reason_code is None
    assert session.deep_assessment is not None
    assert session.deep_assessment.should_escalate is False


def test_structured_signals_are_sent_with_the_final_bundle_profile():
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review")
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.TRIAGE,
        status="completed",
        selected_bundle=ReviewBundle.AUTOTEST,
    )

    profiles = REVIEW_BUNDLES[ReviewBundle.AUTOTEST]
    for index, profile in enumerate(profiles):
        session = orchestrator.advance(
            run_id=session.run_id,
            completed_step=session.current_step,
            status="completed",
            completed_profile=profile,
            risk_signals=(
                _evidenced_signals(cross_system_scope=True)
                if index == len(profiles) - 1
                else None
            ),
        )

    assert session.current_step is OrchestrationStep.SYNTHESIS
    assert session.deep_assessment is not None
    assert session.deep_assessment.should_escalate is False


@pytest.mark.parametrize("profile", list(ReviewAgent))
def test_all_review_profiles_are_retained_after_triage(profile: ReviewAgent):
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review")

    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.TRIAGE,
        status="completed",
        selected_profile=profile,
    )

    assert session.selected_profile is profile
    assert session.selected_bundle is None
    assert session.review_profiles == [profile]
    assert session.current_profile is profile
    assert session.completed_profiles == []
    assert session.current_step is OrchestrationStep.PRIMARY_REVIEW
    assert session.model_policy.model is not None
    assert session.model_policy.model.value == "gpt-6-sol"
    assert session.model_policy.reasoning == "medium"


@pytest.mark.parametrize("bundle", list(ReviewBundle))
def test_fixed_bundle_order_is_retained_after_triage(bundle: ReviewBundle):
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review")

    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.TRIAGE,
        status="completed",
        selected_bundle=bundle,
    )

    assert session.selected_bundle is bundle
    assert session.selected_profile is REVIEW_BUNDLES[bundle][0]
    assert session.review_profiles == list(REVIEW_BUNDLES[bundle])
    assert session.current_profile is REVIEW_BUNDLES[bundle][0]
    assert session.completed_profiles == []
    assert session.current_step is OrchestrationStep.PRIMARY_REVIEW
    assert session.model_policy.model.value == "gpt-6-sol"
    assert session.model_policy.reasoning == "medium"


def test_bundle_requires_each_profile_in_fixed_order_before_synthesis():
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review")
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.TRIAGE,
        status="completed",
        selected_bundle=ReviewBundle.ORDINARY_MR,
    )

    assert session.current_profile is ReviewAgent.CODE_EXPLORER

    with pytest.raises(OrchestrationError, match="final primary-review profile"):
        orchestrator.advance(
            run_id=session.run_id,
            completed_step=OrchestrationStep.PRIMARY_REVIEW,
            status="completed",
            completed_profile=ReviewAgent.CODE_EXPLORER,
            risk_signals=_evidenced_signals(
                high_risk_domain=True,
                evidence_uncertain=True,
            ),
        )

    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.PRIMARY_REVIEW,
        status="completed",
        completed_profile=ReviewAgent.CODE_EXPLORER,
    )
    assert session.current_step is OrchestrationStep.PRIMARY_REVIEW
    assert session.current_profile is ReviewAgent.CODE_REVIEWER
    assert session.completed_profiles == [ReviewAgent.CODE_EXPLORER]

    with pytest.raises(OrchestrationError, match="current profile"):
        orchestrator.advance(
            run_id=session.run_id,
            completed_step=OrchestrationStep.PRIMARY_REVIEW,
            status="completed",
            completed_profile=ReviewAgent.PR_TEST_ANALYZER,
        )

    assert orchestrator.get(session.run_id).current_profile is ReviewAgent.CODE_REVIEWER

    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.PRIMARY_REVIEW,
        status="completed",
        completed_profile=ReviewAgent.CODE_REVIEWER,
    )
    assert session.current_profile is ReviewAgent.PR_TEST_ANALYZER

    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.PRIMARY_REVIEW,
        status="completed",
        completed_profile=ReviewAgent.PR_TEST_ANALYZER,
        risk_signals=DeepReviewSignals(),
    )
    assert session.current_step is OrchestrationStep.SYNTHESIS
    assert session.current_profile is None
    assert session.completed_profiles == list(REVIEW_BUNDLES[ReviewBundle.ORDINARY_MR])


@pytest.mark.parametrize("bundle", list(ReviewBundle))
def test_each_execution_group_can_be_recorded_with_one_transition(bundle: ReviewBundle):
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review")
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.TRIAGE,
        status="completed",
        selected_bundle=bundle,
    )

    groups = bundle_execution_groups(bundle)
    transition_calls = 0
    for index, group in enumerate(groups):
        session = orchestrator.advance(
            run_id=session.run_id,
            completed_step=OrchestrationStep.PRIMARY_REVIEW,
            status="completed",
            completed_profiles=list(group),
            risk_signals=(DeepReviewSignals() if index == len(groups) - 1 else None),
        )
        transition_calls += 1

    assert transition_calls == len(groups)
    if any(len(group) > 1 for group in groups):
        assert transition_calls < len(REVIEW_BUNDLES[bundle])
    assert session.completed_profiles == list(REVIEW_BUNDLES[bundle])
    assert session.current_step is OrchestrationStep.SYNTHESIS


@pytest.mark.parametrize(
    "completed_profiles",
    [
        [ReviewAgent.CODE_REVIEWER],
        [ReviewAgent.PR_TEST_ANALYZER, ReviewAgent.CODE_REVIEWER],
        [ReviewAgent.CODE_EXPLORER, ReviewAgent.CODE_REVIEWER],
    ],
)
def test_group_transition_rejects_incomplete_or_wrong_profiles_without_mutation(
    completed_profiles: list[ReviewAgent],
):
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review")
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.TRIAGE,
        status="completed",
        selected_bundle=ReviewBundle.ORDINARY_MR,
    )

    with pytest.raises(OrchestrationError, match="next execution group"):
        orchestrator.advance(
            run_id=session.run_id,
            completed_step=OrchestrationStep.PRIMARY_REVIEW,
            status="completed",
            completed_profiles=completed_profiles,
        )

    unchanged = orchestrator.get(session.run_id)
    assert unchanged.current_profile is ReviewAgent.CODE_EXPLORER
    assert unchanged.completed_profiles == []


def test_group_transition_cannot_ack_only_the_remaining_suffix_of_a_group():
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review")
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.TRIAGE,
        status="completed",
        selected_bundle=ReviewBundle.ORDINARY_MR,
    )
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.PRIMARY_REVIEW,
        status="completed",
        completed_profile=ReviewAgent.CODE_EXPLORER,
    )

    with pytest.raises(OrchestrationError, match="next execution group"):
        orchestrator.advance(
            run_id=session.run_id,
            completed_step=OrchestrationStep.PRIMARY_REVIEW,
            status="completed",
            completed_profiles=[ReviewAgent.PR_TEST_ANALYZER],
            risk_signals=DeepReviewSignals(),
        )

    unchanged = orchestrator.get(session.run_id)
    assert unchanged.current_profile is ReviewAgent.CODE_REVIEWER
    assert unchanged.completed_profiles == [ReviewAgent.CODE_EXPLORER]


def test_group_transition_requires_risk_signals_for_the_final_group():
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review")
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.TRIAGE,
        status="completed",
        selected_bundle=ReviewBundle.ORDINARY_MR,
    )
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.PRIMARY_REVIEW,
        status="completed",
        completed_profiles=[ReviewAgent.CODE_EXPLORER],
    )

    with pytest.raises(OrchestrationError, match="risk_signals are required"):
        orchestrator.advance(
            run_id=session.run_id,
            completed_step=OrchestrationStep.PRIMARY_REVIEW,
            status="completed",
            completed_profiles=[
                ReviewAgent.CODE_REVIEWER,
                ReviewAgent.PR_TEST_ANALYZER,
            ],
        )

    unchanged = orchestrator.get(session.run_id)
    assert unchanged.current_profile is ReviewAgent.CODE_REVIEWER
    assert unchanged.completed_profiles == [ReviewAgent.CODE_EXPLORER]


def test_clean_single_profile_batch_can_use_existing_fast_path():
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review")
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.TRIAGE,
        status="completed",
        selected_profile=ReviewAgent.CODE_REVIEWER,
    )

    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.PRIMARY_REVIEW,
        status="completed",
        completed_profiles=[ReviewAgent.CODE_REVIEWER],
        risk_signals=DeepReviewSignals(),
        skip_synthesis=True,
    )

    assert session.current_step is OrchestrationStep.AWAITING_HOST_OUTCOME
    assert session.completed_profiles == [ReviewAgent.CODE_REVIEWER]


@pytest.mark.parametrize("bundle", list(ReviewBundle))
def test_every_fixed_bundle_completes_normal_flow(bundle: ReviewBundle):
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review")
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.TRIAGE,
        status="completed",
        selected_bundle=bundle,
    )

    profiles = REVIEW_BUNDLES[bundle]
    for index, profile in enumerate(profiles):
        session = orchestrator.advance(
            run_id=session.run_id,
            completed_step=OrchestrationStep.PRIMARY_REVIEW,
            status="completed",
            completed_profile=profile,
            risk_signals=(DeepReviewSignals() if index == len(profiles) - 1 else None),
        )

    assert session.current_step is OrchestrationStep.SYNTHESIS
    assert session.model_policy.model.value == "gpt-6-sol"
    assert session.model_policy.reasoning == "medium"

    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.SYNTHESIS,
        status="completed",
    )
    completed = orchestrator.finish(run_id=session.run_id, outcome="completed")

    assert session.status is OrchestrationStatus.AWAITING_HOST_OUTCOME
    assert completed.status is OrchestrationStatus.COMPLETED


@pytest.mark.parametrize("bundle", list(ReviewBundle))
def test_every_fixed_bundle_can_escalate_after_final_profile(bundle: ReviewBundle):
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review")
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.TRIAGE,
        status="completed",
        selected_bundle=bundle,
    )

    profiles = REVIEW_BUNDLES[bundle]
    for profile in profiles[:-1]:
        session = orchestrator.advance(
            run_id=session.run_id,
            completed_step=OrchestrationStep.PRIMARY_REVIEW,
            status="completed",
            completed_profile=profile,
        )
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.PRIMARY_REVIEW,
        status="completed",
        completed_profile=profiles[-1],
        risk_signals=_evidenced_signals(
            high_risk_domain=True,
            evidence_uncertain=True,
        ),
    )

    assert session.current_step is OrchestrationStep.DEEP_REVIEW
    assert session.model_policy.model.value == "gpt-6-sol"
    assert session.model_policy.reasoning == "high"

    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.DEEP_REVIEW,
        status="completed",
    )
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.SYNTHESIS,
        status="completed",
    )

    assert session.status is OrchestrationStatus.AWAITING_HOST_OUTCOME


def test_deep_reason_is_retained_in_content_free_session():
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review")
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.TRIAGE,
        status="completed",
        selected_profile=ReviewAgent.SECURITY_REVIEWER,
    )
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.PRIMARY_REVIEW,
        status="completed",
        completed_profile=ReviewAgent.SECURITY_REVIEWER,
        risk_signals=_evidenced_signals(
            high_risk_domain=True,
            evidence_uncertain=True,
        ),
    )

    assert session.deep_reason_code is OrchestrationReason.HIGH_RISK_DOMAIN

    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.DEEP_REVIEW,
        status="completed",
    )
    assert session.deep_reason_code is OrchestrationReason.HIGH_RISK_DOMAIN


def test_returned_bundle_profile_list_cannot_mutate_store():
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review")
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.TRIAGE,
        status="completed",
        selected_bundle=ReviewBundle.ORDINARY_MR,
    )
    session.review_profiles.clear()

    stored = orchestrator.get(session.run_id)

    assert stored.review_profiles == list(REVIEW_BUNDLES[ReviewBundle.ORDINARY_MR])


def test_profile_or_bundle_cannot_change_after_triage():
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review")
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.TRIAGE,
        status="completed",
        selected_bundle=ReviewBundle.ORDINARY_MR,
    )

    with pytest.raises(OrchestrationError, match="invalid transition signal"):
        orchestrator.advance(
            run_id=session.run_id,
            completed_step=OrchestrationStep.PRIMARY_REVIEW,
            status="completed",
            completed_profile=ReviewAgent.CODE_EXPLORER,
            selected_profile=ReviewAgent.SECURITY_REVIEWER,
        )


def test_illegal_transition_does_not_mutate_session():
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review")

    with pytest.raises(OrchestrationError, match="illegal transition"):
        orchestrator.advance(
            run_id=session.run_id,
            completed_step=OrchestrationStep.PRIMARY_REVIEW,
            status="completed",
            completed_profile=ReviewAgent.CODE_REVIEWER,
        )

    current = orchestrator.get(session.run_id)
    assert current.current_step is OrchestrationStep.TRIAGE
    assert current.status is OrchestrationStatus.ACTIVE


def test_partial_or_blocked_step_is_terminal():
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review")

    blocked = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.TRIAGE,
        status="blocked",
    )

    assert blocked.status is OrchestrationStatus.BLOCKED
    with pytest.raises(OrchestrationError, match="terminal"):
        orchestrator.advance(
            run_id=session.run_id,
            completed_step=OrchestrationStep.TRIAGE,
            status="completed",
            selected_profile="code_reviewer",
        )


def test_expired_session_is_removed():
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    orchestrator = QaOrchestrator(
        ttl_seconds=60,
        max_sessions=10,
        clock=lambda: now,
    )
    session = orchestrator.start("ordinary_review")

    now = now + timedelta(seconds=61)

    with pytest.raises(OrchestrationError, match="expired session"):
        orchestrator.get(session.run_id)

    with pytest.raises(OrchestrationError, match="expired session"):
        orchestrator.advance(
            run_id=session.run_id,
            completed_step=OrchestrationStep.TRIAGE,
            status="completed",
            selected_profile="code_reviewer",
        )


def test_session_limit_allows_new_session_after_expiry():
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    orchestrator = QaOrchestrator(
        ttl_seconds=60,
        max_sessions=2,
        clock=lambda: now,
    )
    orchestrator.start("ordinary_review")
    orchestrator.start("widget_review")

    with pytest.raises(OrchestrationError, match="session limit"):
        orchestrator.start("qa_planning")

    now = now + timedelta(seconds=61)

    new_session = orchestrator.start("qa_planning")
    assert new_session.status is OrchestrationStatus.ACTIVE


def test_returned_session_cannot_mutate_store():
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review")
    session.next_action = "tampered"

    stored = orchestrator.get(session.run_id)

    assert stored.next_action != "tampered"
