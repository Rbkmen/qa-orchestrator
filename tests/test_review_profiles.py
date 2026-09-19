import pytest

from qa_router_mcp.contracts import ReviewAgent, ReviewRoute
from qa_router_mcp.service import RouterService


def test_every_review_profile_returns_read_only_route(tmp_path):
    service = RouterService.from_settings(data_dir=tmp_path)

    for profile in ReviewAgent:
        route = service.prepare_review_route(profile)

        assert isinstance(route, ReviewRoute)
        assert route.profile == profile
        assert route.read_only is True
        assert route.host_owns_decisions is True
        assert route.required_sections == [
            "Scope",
            "Checklist",
            "Candidate Coverage Gaps",
            "Positive Observations",
            "Unverified",
        ]
        assert route.focus


def test_unknown_review_profile_is_rejected(tmp_path):
    service = RouterService.from_settings(data_dir=tmp_path)

    with pytest.raises(ValueError, match="unknown review agent profile"):
        service.prepare_review_route("unknown_profile")
