from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from qa_router_mcp.contracts import ReviewAgent, ReviewBundle
from qa_router_mcp.orchestration import (
    OrchestrationError,
    OrchestrationStatus,
    OrchestrationStep,
    QaOrchestrator,
)
from qa_router_mcp.review_profiles import REVIEW_BUNDLES


def test_normal_flow_skips_sol():
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review")

    assert session.current_step is OrchestrationStep.LUNA_TRIAGE
    assert session.model_policy.model.value == "gpt-5.6-luna"
    assert session.model_policy.reasoning == "max"

    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.LUNA_TRIAGE,
        status="completed",
        selected_profile="code_reviewer",
    )
    assert session.current_step is OrchestrationStep.TERRA_PRIMARY_REVIEW
    assert session.model_policy.reasoning == "medium"

    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.TERRA_PRIMARY_REVIEW,
        status="completed",
        completed_profile=ReviewAgent.CODE_REVIEWER,
        needs_deep_analysis=False,
    )
    assert session.current_step is OrchestrationStep.TERRA_SYNTHESIS

    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.TERRA_SYNTHESIS,
        status="completed",
    )
    assert session.status is OrchestrationStatus.AWAITING_HOST_OUTCOME
    assert session.current_step is OrchestrationStep.AWAITING_HOST_OUTCOME
    assert session.model_policy is None


def test_final_host_outcome_completes_session_and_is_idempotent():
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review")
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.LUNA_TRIAGE,
        status="completed",
        selected_profile="code_reviewer",
    )
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.TERRA_PRIMARY_REVIEW,
        status="completed",
        completed_profile=ReviewAgent.CODE_REVIEWER,
    )
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.TERRA_SYNTHESIS,
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
        completed_step=OrchestrationStep.LUNA_TRIAGE,
        status="completed",
        selected_profile="code_reviewer",
    )
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.TERRA_PRIMARY_REVIEW,
        status="completed",
        completed_profile=ReviewAgent.CODE_REVIEWER,
    )
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.TERRA_SYNTHESIS,
        status="completed",
    )

    partial = orchestrator.finish(run_id=session.run_id, outcome="partial")

    assert partial.status is OrchestrationStatus.PARTIAL


def test_terminal_session_does_not_consume_active_session_limit():
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=1)
    session = orchestrator.start("ordinary_review")

    blocked = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.LUNA_TRIAGE,
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


def test_deep_flow_uses_sol_then_returns_to_terra():
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review")
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.LUNA_TRIAGE,
        status="completed",
        selected_profile="security_reviewer",
    )
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.TERRA_PRIMARY_REVIEW,
        status="completed",
        completed_profile=ReviewAgent.SECURITY_REVIEWER,
        needs_deep_analysis=True,
        reason_code="security_sensitive",
    )

    assert session.current_step is OrchestrationStep.SOL_DEEP_REVIEW
    assert session.model_policy.model.value == "gpt-5.6-sol"
    assert session.model_policy.reasoning == "high"

    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.SOL_DEEP_REVIEW,
        status="completed",
    )
    assert session.current_step is OrchestrationStep.TERRA_SYNTHESIS


@pytest.mark.parametrize("profile", list(ReviewAgent))
def test_all_review_profiles_are_retained_after_triage(profile: ReviewAgent):
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review")

    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.LUNA_TRIAGE,
        status="completed",
        selected_profile=profile,
    )

    assert session.selected_profile is profile
    assert session.selected_bundle is None
    assert session.review_profiles == [profile]
    assert session.current_profile is profile
    assert session.completed_profiles == []
    assert session.current_step is OrchestrationStep.TERRA_PRIMARY_REVIEW
    assert session.model_policy.model is not None
    assert session.model_policy.model.value == "gpt-5.6-terra"
    assert session.model_policy.reasoning == "medium"


@pytest.mark.parametrize("bundle", list(ReviewBundle))
def test_fixed_bundle_order_is_retained_after_triage(bundle: ReviewBundle):
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review")

    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.LUNA_TRIAGE,
        status="completed",
        selected_bundle=bundle,
    )

    assert session.selected_bundle is bundle
    assert session.selected_profile is REVIEW_BUNDLES[bundle][0]
    assert session.review_profiles == list(REVIEW_BUNDLES[bundle])
    assert session.current_profile is REVIEW_BUNDLES[bundle][0]
    assert session.completed_profiles == []
    assert session.current_step is OrchestrationStep.TERRA_PRIMARY_REVIEW
    assert session.model_policy.model.value == "gpt-5.6-terra"
    assert session.model_policy.reasoning == "medium"


def test_bundle_requires_each_profile_in_fixed_order_before_synthesis():
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review")
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.LUNA_TRIAGE,
        status="completed",
        selected_bundle=ReviewBundle.ORDINARY_MR,
    )

    assert session.current_profile is ReviewAgent.CODE_EXPLORER

    with pytest.raises(OrchestrationError, match="final review profile"):
        orchestrator.advance(
            run_id=session.run_id,
            completed_step=OrchestrationStep.TERRA_PRIMARY_REVIEW,
            status="completed",
            completed_profile=ReviewAgent.CODE_EXPLORER,
            needs_deep_analysis=True,
            reason_code="security_sensitive",
        )

    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.TERRA_PRIMARY_REVIEW,
        status="completed",
        completed_profile=ReviewAgent.CODE_EXPLORER,
    )
    assert session.current_step is OrchestrationStep.TERRA_PRIMARY_REVIEW
    assert session.current_profile is ReviewAgent.CODE_REVIEWER
    assert session.completed_profiles == [ReviewAgent.CODE_EXPLORER]

    with pytest.raises(OrchestrationError, match="current profile"):
        orchestrator.advance(
            run_id=session.run_id,
            completed_step=OrchestrationStep.TERRA_PRIMARY_REVIEW,
            status="completed",
            completed_profile=ReviewAgent.PR_TEST_ANALYZER,
        )

    assert orchestrator.get(session.run_id).current_profile is ReviewAgent.CODE_REVIEWER

    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.TERRA_PRIMARY_REVIEW,
        status="completed",
        completed_profile=ReviewAgent.CODE_REVIEWER,
    )
    assert session.current_profile is ReviewAgent.PR_TEST_ANALYZER

    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.TERRA_PRIMARY_REVIEW,
        status="completed",
        completed_profile=ReviewAgent.PR_TEST_ANALYZER,
    )
    assert session.current_step is OrchestrationStep.TERRA_SYNTHESIS
    assert session.current_profile is None
    assert session.completed_profiles == list(REVIEW_BUNDLES[ReviewBundle.ORDINARY_MR])


def test_deep_reason_is_retained_in_content_free_session():
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review")
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.LUNA_TRIAGE,
        status="completed",
        selected_profile=ReviewAgent.SECURITY_REVIEWER,
    )
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.TERRA_PRIMARY_REVIEW,
        status="completed",
        completed_profile=ReviewAgent.SECURITY_REVIEWER,
        needs_deep_analysis=True,
        reason_code="security_sensitive",
    )

    assert session.deep_reason_code.value == "security_sensitive"

    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.SOL_DEEP_REVIEW,
        status="completed",
    )
    assert session.deep_reason_code.value == "security_sensitive"


def test_returned_bundle_profile_list_cannot_mutate_store():
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review")
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.LUNA_TRIAGE,
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
        completed_step=OrchestrationStep.LUNA_TRIAGE,
        status="completed",
        selected_bundle=ReviewBundle.ORDINARY_MR,
    )

    with pytest.raises(OrchestrationError, match="invalid transition signal"):
        orchestrator.advance(
            run_id=session.run_id,
            completed_step=OrchestrationStep.TERRA_PRIMARY_REVIEW,
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
            completed_step=OrchestrationStep.TERRA_PRIMARY_REVIEW,
            status="completed",
            completed_profile=ReviewAgent.CODE_REVIEWER,
        )

    current = orchestrator.get(session.run_id)
    assert current.current_step is OrchestrationStep.LUNA_TRIAGE
    assert current.status is OrchestrationStatus.ACTIVE


def test_partial_or_blocked_step_is_terminal():
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review")

    blocked = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.LUNA_TRIAGE,
        status="blocked",
    )

    assert blocked.status is OrchestrationStatus.BLOCKED
    with pytest.raises(OrchestrationError, match="terminal"):
        orchestrator.advance(
            run_id=session.run_id,
            completed_step=OrchestrationStep.LUNA_TRIAGE,
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
            completed_step=OrchestrationStep.LUNA_TRIAGE,
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
