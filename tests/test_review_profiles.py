import pytest

from qa_router_mcp import contracts, review_profiles
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


def test_review_profiles_expose_the_approved_display_names(tmp_path):
    service = RouterService.from_settings(data_dir=tmp_path)
    expected_names = {
        ReviewAgent.CODE_EXPLORER: "Faraday — Evidence Investigator",
        ReviewAgent.CODE_REVIEWER: "Code Reviewer",
        ReviewAgent.PR_TEST_ANALYZER: "Test Analyzer",
        ReviewAgent.SECURITY_REVIEWER: "Security Reviewer",
        ReviewAgent.SILENT_FAILURE_HUNTER: "Silent Failure Hunter",
        ReviewAgent.TYPESCRIPT_REVIEWER: "TypeScript Reviewer",
        ReviewAgent.REACT_REVIEWER: "React Reviewer",
    }

    for profile, expected_name in expected_names.items():
        route = service.prepare_review_route(profile)
        assert getattr(route, "display_name", None) == expected_name


def test_review_bundle_catalog_has_the_approved_immutable_order():
    bundle_type = getattr(contracts, "ReviewBundle", None)
    assert bundle_type is not None

    expected_bundles = {
        bundle_type.ORDINARY_MR: (
            ReviewAgent.CODE_EXPLORER,
            ReviewAgent.CODE_REVIEWER,
            ReviewAgent.PR_TEST_ANALYZER,
        ),
        bundle_type.WIDGET: (
            ReviewAgent.CODE_EXPLORER,
            ReviewAgent.REACT_REVIEWER,
            ReviewAgent.TYPESCRIPT_REVIEWER,
            ReviewAgent.PR_TEST_ANALYZER,
        ),
        bundle_type.SECURITY: (
            ReviewAgent.CODE_EXPLORER,
            ReviewAgent.SECURITY_REVIEWER,
            ReviewAgent.SILENT_FAILURE_HUNTER,
        ),
        bundle_type.AUTOTEST: (
            ReviewAgent.CODE_REVIEWER,
            ReviewAgent.PR_TEST_ANALYZER,
            ReviewAgent.TYPESCRIPT_REVIEWER,
        ),
        bundle_type.REQUIREMENTS: (
            ReviewAgent.CODE_EXPLORER,
            ReviewAgent.CODE_REVIEWER,
        ),
    }

    catalog = getattr(review_profiles, "REVIEW_BUNDLES", None)
    assert catalog == expected_bundles
    assert all(isinstance(profiles, tuple) for profiles in catalog.values())


def test_unknown_review_bundle_is_rejected_by_the_typed_contract():
    bundle_type = getattr(contracts, "ReviewBundle", None)
    assert bundle_type is not None

    with pytest.raises(ValueError):
        bundle_type("unknown_bundle")
