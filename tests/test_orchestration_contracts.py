import pytest
from pydantic import ValidationError

from qa_router_mcp.contracts import ReviewAgent
from qa_router_mcp.orchestration import (
    AdvanceQaOrchestrationRequest,
    MODEL_POLICIES,
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
            needs_deep_analysis=True,
        )

    request = AdvanceQaOrchestrationRequest(
        run_id="qar-0123456789abcdef0123456789abcdef",
        completed_step=OrchestrationStep.TERRA_PRIMARY_REVIEW,
        status="completed",
        needs_deep_analysis=True,
        reason_code=OrchestrationReason.SECURITY_SENSITIVE,
    )
    assert request.reason_code is OrchestrationReason.SECURITY_SENSITIVE


def test_non_deep_request_rejects_reason_code():
    with pytest.raises(ValidationError):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.TERRA_PRIMARY_REVIEW,
            status="completed",
            reason_code=OrchestrationReason.ROOT_CAUSE,
        )
