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
    OrchestrationReason,
    OrchestrationStatus,
    OrchestrationStep,
    QaOrchestrationSession,
)


@pytest.mark.parametrize(
    ("step", "model", "reasoning"),
    [
        (OrchestrationStep.LUNA_TRIAGE, "gpt-6-luna", "max"),
        (OrchestrationStep.TERRA_PRIMARY_REVIEW, "gpt-6-sol", "medium"),
        (OrchestrationStep.SOL_DEEP_REVIEW, "gpt-6-sol", "high"),
        (OrchestrationStep.TERRA_SYNTHESIS, "gpt-6-sol", "medium"),
    ],
)
def test_model_policy_assigns_requested_models_and_reasoning(step, model, reasoning):
    policy = MODEL_POLICIES[step]
    assert policy.model.value == model
    assert policy.reasoning == reasoning
    assert policy.speed == 1.0
    assert set(MODEL_POLICIES) == {
        OrchestrationStep.LUNA_TRIAGE,
        OrchestrationStep.TERRA_PRIMARY_REVIEW,
        OrchestrationStep.SOL_DEEP_REVIEW,
        OrchestrationStep.TERRA_SYNTHESIS,
    }


def test_model_policy_pins_unit_speed():
    assert {policy.speed for policy in MODEL_POLICIES.values()} == {1.0}

    with pytest.raises(ValidationError):
        ModelPolicy(model=OrchestrationModel.SOL, reasoning="medium", speed=1.5)


def test_orchestration_session_rejects_disabled_ownership_flags():
    with pytest.raises(ValidationError):
        QaOrchestrationSession(
            run_id="qar-0123456789abcdef0123456789abcdef",
            task_type="ordinary_review",
            status=OrchestrationStatus.ACTIVE,
            current_step=OrchestrationStep.LUNA_TRIAGE,
            next_action="Host runs Luna triage.",
            read_only=False,
        )

    with pytest.raises(ValidationError):
        QaOrchestrationSession(
            run_id="qar-0123456789abcdef0123456789abcdef",
            task_type="ordinary_review",
            status=OrchestrationStatus.ACTIVE,
            current_step=OrchestrationStep.LUNA_TRIAGE,
            next_action="Host runs Luna triage.",
            host_owns_decisions=False,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )


def test_advance_request_rejects_free_form_fields():
    with pytest.raises(ValidationError):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.LUNA_TRIAGE,
            status="completed",
            selected_profile=ReviewAgent.CODE_REVIEWER,
            evidence="raw source text",
        )


def test_deep_reason_requires_fixed_reason_code():
    with pytest.raises(ValidationError):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.TERRA_PRIMARY_REVIEW,
            status="completed",
            completed_profile=ReviewAgent.CODE_REVIEWER,
            needs_deep_analysis=True,
        )

    request = AdvanceQaOrchestrationRequest(
        run_id="qar-0123456789abcdef0123456789abcdef",
        completed_step=OrchestrationStep.TERRA_PRIMARY_REVIEW,
        status="completed",
        completed_profile=ReviewAgent.CODE_REVIEWER,
        needs_deep_analysis=True,
        reason_code=OrchestrationReason.SECURITY_SENSITIVE,
    )
    assert request.reason_code is OrchestrationReason.SECURITY_SENSITIVE


def test_risk_signals_are_final_terra_only_and_replace_manual_deep_request():
    signals = DeepReviewSignals(high_risk_domain=True, evidence_uncertain=True)

    with pytest.raises(ValidationError, match="final Terra primary profile"):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.LUNA_TRIAGE,
            status="completed",
            selected_profile=ReviewAgent.CODE_REVIEWER,
            risk_signals=signals,
        )

    with pytest.raises(ValidationError, match="final Terra primary profile"):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.TERRA_SYNTHESIS,
            status="completed",
            risk_signals=signals,
        )

    with pytest.raises(ValidationError, match="cannot be combined"):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.TERRA_PRIMARY_REVIEW,
            status="completed",
            completed_profile=ReviewAgent.CODE_REVIEWER,
            risk_signals=signals,
            needs_deep_analysis=True,
        )


def test_risk_signals_require_a_completed_profile():
    with pytest.raises(ValidationError, match="completed_profile"):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.TERRA_PRIMARY_REVIEW,
            status="completed",
            risk_signals=DeepReviewSignals(cross_system_scope=True),
        )


def test_completed_terra_requires_completed_profile():
    with pytest.raises(ValidationError, match="completed_profile"):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.TERRA_PRIMARY_REVIEW,
            status="completed",
        )


def test_completed_profile_is_rejected_outside_terra():
    with pytest.raises(ValidationError, match="only be supplied"):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.LUNA_TRIAGE,
            status="completed",
            selected_profile=ReviewAgent.CODE_REVIEWER,
            completed_profile=ReviewAgent.CODE_REVIEWER,
        )


def test_partial_terra_rejects_ignored_completed_profile():
    with pytest.raises(ValidationError, match="completed_profile"):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.TERRA_PRIMARY_REVIEW,
            status="partial",
            completed_profile=ReviewAgent.CODE_REVIEWER,
        )


def test_non_deep_request_rejects_reason_code():
    with pytest.raises(ValidationError):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.TERRA_PRIMARY_REVIEW,
            status="completed",
            completed_profile=ReviewAgent.CODE_REVIEWER,
            reason_code=OrchestrationReason.ROOT_CAUSE,
        )


def test_completed_luna_requires_exactly_one_selection():
    with pytest.raises(ValidationError, match="exactly one"):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.LUNA_TRIAGE,
            status="completed",
        )

    with pytest.raises(ValidationError, match="exactly one"):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.LUNA_TRIAGE,
            status="completed",
            selected_profile=ReviewAgent.CODE_REVIEWER,
            selected_bundle=ReviewBundle.ORDINARY_MR,
        )


def test_completed_luna_accepts_one_fixed_bundle():
    request = AdvanceQaOrchestrationRequest(
        run_id="qar-0123456789abcdef0123456789abcdef",
        completed_step=OrchestrationStep.LUNA_TRIAGE,
        status="completed",
        selected_bundle=ReviewBundle.ORDINARY_MR,
    )

    assert request.selected_bundle is ReviewBundle.ORDINARY_MR


def test_selection_is_rejected_after_luna():
    with pytest.raises(ValidationError, match="only be supplied after Luna"):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.TERRA_PRIMARY_REVIEW,
            status="completed",
            completed_profile=ReviewAgent.CODE_REVIEWER,
            selected_bundle=ReviewBundle.ORDINARY_MR,
        )


def test_arbitrary_profile_order_is_rejected():
    with pytest.raises(ValidationError):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.LUNA_TRIAGE,
            status="completed",
            selected_profile=ReviewAgent.CODE_REVIEWER,
            review_profiles=[ReviewAgent.CODE_REVIEWER, ReviewAgent.CODE_EXPLORER],
        )
