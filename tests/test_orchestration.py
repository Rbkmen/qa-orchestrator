from datetime import UTC, datetime, timedelta

import pytest

from qa_router_mcp.contracts import ReviewAgent
from qa_router_mcp.orchestration import (
    OrchestrationError,
    OrchestrationStatus,
    OrchestrationStep,
    QaOrchestrator,
)


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
    assert session.current_step is OrchestrationStep.TERRA_PRIMARY_REVIEW
    assert session.model_policy.model is not None
    assert session.model_policy.model.value == "gpt-5.6-terra"
    assert session.model_policy.reasoning == "medium"


def test_illegal_transition_does_not_mutate_session():
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review")

    with pytest.raises(OrchestrationError, match="illegal transition"):
        orchestrator.advance(
            run_id=session.run_id,
            completed_step=OrchestrationStep.TERRA_PRIMARY_REVIEW,
            status="completed",
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
