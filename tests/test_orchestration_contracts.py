import pytest
from pydantic import ValidationError

from qa_router_mcp.contracts import ReviewAgent, ReviewBundle
from qa_router_mcp.orchestration import (
    MODEL_POLICIES,
    AdvanceQaOrchestrationRequest,
    OrchestrationModel,
    OrchestrationReason,
    OrchestrationStep,
)


def test_model_policy_assigns_requested_models_and_reasoning():
    assert MODEL_POLICIES[OrchestrationStep.LUNA_TRIAGE].model == OrchestrationModel.LUNA
    assert MODEL_POLICIES[OrchestrationStep.LUNA_TRIAGE].reasoning == "max"
    assert MODEL_POLICIES[OrchestrationStep.TERRA_PRIMARY_REVIEW].model == OrchestrationModel.TERRA
    assert MODEL_POLICIES[OrchestrationStep.TERRA_PRIMARY_REVIEW].reasoning == "medium"
    assert MODEL_POLICIES[OrchestrationStep.SOL_DEEP_REVIEW].model == OrchestrationModel.SOL
    assert MODEL_POLICIES[OrchestrationStep.SOL_DEEP_REVIEW].reasoning == "high"
    assert set(MODEL_POLICIES) == {
        OrchestrationStep.LUNA_TRIAGE,
        OrchestrationStep.TERRA_PRIMARY_REVIEW,
        OrchestrationStep.SOL_DEEP_REVIEW,
    }


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
