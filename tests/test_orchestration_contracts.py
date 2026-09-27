from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from qa_orchestrator.contracts import ReviewAgent, ReviewBundle
from qa_orchestrator.orchestration import (
    MODEL_POLICIES,
    AdvanceQaOrchestrationRequest,
    DeepReviewSignals,
    ModelPolicy,
    OrchestrationModel,
    OrchestrationStatus,
    OrchestrationStep,
    QaOrchestrationSession,
)


@pytest.mark.parametrize(
    ("step", "model", "reasoning"),
    [
        (OrchestrationStep.TRIAGE, "gpt-6-luna", "max"),
        (OrchestrationStep.PRIMARY_REVIEW, "gpt-6-sol", "medium"),
        (OrchestrationStep.DEEP_REVIEW, "gpt-6-sol", "high"),
        (OrchestrationStep.SYNTHESIS, "gpt-6-sol", "medium"),
    ],
)
def test_model_policy_assigns_requested_models_and_reasoning(step, model, reasoning):
    policy = MODEL_POLICIES[step]
    assert policy.model.value == model
    assert policy.reasoning == reasoning
    assert set(policy.model_dump(mode="json")) == {"provider", "model", "reasoning"}
    assert set(MODEL_POLICIES) == {
        OrchestrationStep.TRIAGE,
        OrchestrationStep.PRIMARY_REVIEW,
        OrchestrationStep.DEEP_REVIEW,
        OrchestrationStep.SYNTHESIS,
    }


def test_model_policy_rejects_orchestrator_speed_override():
    assert all("speed" not in policy.model_dump() for policy in MODEL_POLICIES.values())

    with pytest.raises(ValidationError):
        ModelPolicy(model=OrchestrationModel.SOL, reasoning="medium", speed=1.5)


def test_orchestration_session_rejects_disabled_ownership_flags():
    with pytest.raises(ValidationError):
        QaOrchestrationSession(
            run_id="qar-0123456789abcdef0123456789abcdef",
            task_type="ordinary_review",
            status=OrchestrationStatus.ACTIVE,
            current_step=OrchestrationStep.TRIAGE,
            next_action="Host performs triage.",
            read_only=False,
        )

    with pytest.raises(ValidationError):
        QaOrchestrationSession(
            run_id="qar-0123456789abcdef0123456789abcdef",
            task_type="ordinary_review",
            status=OrchestrationStatus.ACTIVE,
            current_step=OrchestrationStep.TRIAGE,
            next_action="Host performs triage.",
            host_owns_decisions=False,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )


def test_advance_request_rejects_free_form_fields():
    with pytest.raises(ValidationError):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.TRIAGE,
            status="completed",
            selected_profile=ReviewAgent.CODE_REVIEWER,
            evidence="raw source text",
        )


@pytest.mark.parametrize(
    "step",
    ("luna_triage", "terra_primary_review", "sol_deep_review", "terra_synthesis"),
)
def test_transition_identifiers_are_model_neutral(step):
    with pytest.raises(ValueError):
        OrchestrationStep(step)


def test_legacy_manual_escalation_fields_are_rejected():
    with pytest.raises(ValidationError):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.PRIMARY_REVIEW,
            status="completed",
            completed_profile=ReviewAgent.CODE_REVIEWER,
            needs_deep_analysis=True,
            reason_code="high_risk_domain",
        )


def test_risk_signals_are_final_primary_review_only():
    signals = DeepReviewSignals(high_risk_domain=True, evidence_uncertain=True)

    with pytest.raises(ValidationError, match="final primary-review profile"):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.TRIAGE,
            status="completed",
            selected_profile=ReviewAgent.CODE_REVIEWER,
            risk_signals=signals,
        )

    with pytest.raises(ValidationError, match="final primary-review profile"):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.SYNTHESIS,
            status="completed",
            risk_signals=signals,
        )

def test_risk_signals_require_a_completed_profile():
    with pytest.raises(ValidationError, match="completed_profile"):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.PRIMARY_REVIEW,
            status="completed",
            risk_signals=DeepReviewSignals(cross_system_scope=True),
        )


def test_completed_primary_review_requires_completed_profile():
    with pytest.raises(ValidationError, match="completed_profile"):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.PRIMARY_REVIEW,
            status="completed",
        )


def test_completed_profile_is_rejected_outside_primary_review():
    with pytest.raises(ValidationError, match="only be supplied"):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.TRIAGE,
            status="completed",
            selected_profile=ReviewAgent.CODE_REVIEWER,
            completed_profile=ReviewAgent.CODE_REVIEWER,
        )


def test_partial_primary_review_rejects_ignored_completed_profile():
    with pytest.raises(ValidationError, match="completed_profile"):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.PRIMARY_REVIEW,
            status="partial",
            completed_profile=ReviewAgent.CODE_REVIEWER,
        )


def test_completed_triage_requires_exactly_one_selection():
    with pytest.raises(ValidationError, match="exactly one"):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.TRIAGE,
            status="completed",
        )

    with pytest.raises(ValidationError, match="exactly one"):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.TRIAGE,
            status="completed",
            selected_profile=ReviewAgent.CODE_REVIEWER,
            selected_bundle=ReviewBundle.ORDINARY_MR,
        )


def test_completed_triage_accepts_one_fixed_bundle():
    request = AdvanceQaOrchestrationRequest(
        run_id="qar-0123456789abcdef0123456789abcdef",
        completed_step=OrchestrationStep.TRIAGE,
        status="completed",
        selected_bundle=ReviewBundle.ORDINARY_MR,
    )

    assert request.selected_bundle is ReviewBundle.ORDINARY_MR


def test_selection_is_rejected_after_triage():
    with pytest.raises(ValidationError, match="only be supplied after triage"):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.PRIMARY_REVIEW,
            status="completed",
            completed_profile=ReviewAgent.CODE_REVIEWER,
            selected_bundle=ReviewBundle.ORDINARY_MR,
        )


def test_arbitrary_profile_order_is_rejected():
    with pytest.raises(ValidationError):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.TRIAGE,
            status="completed",
            selected_profile=ReviewAgent.CODE_REVIEWER,
            review_profiles=[ReviewAgent.CODE_REVIEWER, ReviewAgent.CODE_EXPLORER],
        )
