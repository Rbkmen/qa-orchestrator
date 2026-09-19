import pytest
from pydantic import ValidationError

from qa_router_mcp.contracts import QaTaskOutcomeReceipt, ReviewAgent, ReviewRoute


def test_review_route_requires_focus_and_sections():
    route = ReviewRoute(
        profile=ReviewAgent.CODE_REVIEWER,
        focus="changed surface",
        required_sections=["Scope"],
        constraints=["read only"],
    )

    assert route.read_only is True
    assert route.host_owns_decisions is True


def test_review_route_requires_non_empty_focus():
    with pytest.raises(ValidationError):
        ReviewRoute(
            profile=ReviewAgent.CODE_REVIEWER,
            focus="",
            required_sections=["Scope"],
            constraints=["read only"],
        )


def test_task_outcome_receipt_has_only_recording_status():
    receipt = QaTaskOutcomeReceipt(status="recorded")

    assert receipt.model_dump() == {"status": "recorded"}
