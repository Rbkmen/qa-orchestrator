import pytest
from pydantic import ValidationError

from qa_orchestrator.contracts import QaTaskOutcomeReceipt, ReviewAgent, ReviewRoute


def test_review_route_requires_focus_and_sections():
    route = ReviewRoute(
        profile=ReviewAgent.CODE_REVIEWER,
        display_name="Code Reviewer",
        focus="changed surface",
        required_sections=["Scope"],
        constraints=["read only"],
    )

    assert route.read_only is True
    assert route.host_owns_decisions is True


def test_review_route_requires_non_empty_focus():
    with pytest.raises(ValidationError) as error:
        ReviewRoute(
            profile=ReviewAgent.CODE_REVIEWER,
            display_name="Code Reviewer",
            focus="",
            required_sections=["Scope"],
            constraints=["read only"],
        )

    assert error.value.errors()[0]["loc"] == ("focus",)


def test_review_route_rejects_disabled_ownership_flags():
    with pytest.raises(ValidationError):
        ReviewRoute(
            profile=ReviewAgent.CODE_REVIEWER,
            display_name="Code Reviewer",
            focus="changed surface",
            required_sections=["Scope"],
            constraints=["read only"],
            read_only=False,
        )

    with pytest.raises(ValidationError):
        ReviewRoute(
            profile=ReviewAgent.CODE_REVIEWER,
            display_name="Code Reviewer",
            focus="changed surface",
            required_sections=["Scope"],
            constraints=["read only"],
            host_owns_decisions=False,
        )


def test_task_outcome_receipt_has_only_recording_status():
    receipt = QaTaskOutcomeReceipt(status="recorded")

    assert receipt.model_dump() == {"status": "recorded"}
