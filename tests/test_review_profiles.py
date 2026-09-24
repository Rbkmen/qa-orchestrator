import pytest

from qa_orchestrator import contracts, review_profiles
from qa_orchestrator.contracts import ReviewAgent, ReviewRoute
from qa_orchestrator.service import OrchestratorService


def test_every_review_profile_returns_read_only_route(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    expected_sections = {
        ReviewAgent.CODE_EXPLORER: ["Scope", "Evidence Map", "Unverified"],
        ReviewAgent.PR_TEST_ANALYZER: ["Scope", "Coverage Gaps", "Unverified"],
        ReviewAgent.CODE_REVIEWER: [
            "Scope",
            "Finding Candidates",
            "Coverage Gaps",
            "Unverified",
        ],
        ReviewAgent.SECURITY_REVIEWER: [
            "Scope",
            "Finding Candidates",
            "Coverage Gaps",
            "Unverified",
        ],
        ReviewAgent.SILENT_FAILURE_HUNTER: [
            "Scope",
            "Finding Candidates",
            "Coverage Gaps",
            "Unverified",
        ],
        ReviewAgent.TYPESCRIPT_REVIEWER: [
            "Scope",
            "Finding Candidates",
            "Coverage Gaps",
            "Unverified",
        ],
        ReviewAgent.REACT_REVIEWER: [
            "Scope",
            "Finding Candidates",
            "Coverage Gaps",
            "Unverified",
        ],
        ReviewAgent.RUBY_REVIEWER: [
            "Scope",
            "Finding Candidates",
            "Coverage Gaps",
            "Unverified",
        ],
        ReviewAgent.PYTHON_REVIEWER: [
            "Scope",
            "Finding Candidates",
            "Coverage Gaps",
            "Unverified",
        ],
        ReviewAgent.MOBILE_REVIEWER: [
            "Scope",
            "Finding Candidates",
            "Coverage Gaps",
            "Unverified",
        ],
    }

    for profile in ReviewAgent:
        route = service.prepare_review_route(profile)

        assert isinstance(route, ReviewRoute)
        assert route.profile == profile
        assert route.read_only is True
        assert route.host_owns_decisions is True
        assert route.required_sections == expected_sections[profile]
        assert route.focus


def test_evidence_investigator_and_code_reviewer_have_distinct_outputs(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    evidence_route = service.prepare_review_route(ReviewAgent.CODE_EXPLORER)
    review_route = service.prepare_review_route(ReviewAgent.CODE_REVIEWER)

    assert "Evidence Map" in evidence_route.required_sections
    assert "Finding Candidates" not in evidence_route.required_sections
    assert "Finding Candidates" in review_route.required_sections
    assert "Evidence Map" not in review_route.required_sections
    assert "do not diagnose defects" in evidence_route.focus
    assert "do not repeat the map" in review_route.focus


def test_unknown_review_profile_is_rejected(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    with pytest.raises(ValueError, match="unknown review agent profile"):
        service.prepare_review_route("unknown_profile")


def test_review_profiles_expose_the_approved_display_names(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    expected_names = {
        ReviewAgent.CODE_EXPLORER: "Faraday — Evidence Investigator",
        ReviewAgent.CODE_REVIEWER: "Code Reviewer",
        ReviewAgent.PR_TEST_ANALYZER: "Test Analyzer",
        ReviewAgent.SECURITY_REVIEWER: "Security Reviewer",
        ReviewAgent.SILENT_FAILURE_HUNTER: "Silent Failure Hunter",
        ReviewAgent.TYPESCRIPT_REVIEWER: "TypeScript Reviewer",
        ReviewAgent.REACT_REVIEWER: "React Reviewer",
        ReviewAgent.RUBY_REVIEWER: "Ruby Reviewer",
        ReviewAgent.PYTHON_REVIEWER: "Python Reviewer",
        ReviewAgent.MOBILE_REVIEWER: "Mobile Reviewer",
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
        bundle_type.WIDGET_JS: (
            ReviewAgent.CODE_EXPLORER,
            ReviewAgent.CODE_REVIEWER,
            ReviewAgent.REACT_REVIEWER,
            ReviewAgent.PR_TEST_ANALYZER,
        ),
        bundle_type.RUBY_BACKEND: (
            ReviewAgent.CODE_EXPLORER,
            ReviewAgent.RUBY_REVIEWER,
            ReviewAgent.PR_TEST_ANALYZER,
        ),
        bundle_type.PYTHON_BACKEND: (
            ReviewAgent.CODE_EXPLORER,
            ReviewAgent.PYTHON_REVIEWER,
            ReviewAgent.PR_TEST_ANALYZER,
        ),
        bundle_type.MOBILE: (
            ReviewAgent.CODE_EXPLORER,
            ReviewAgent.MOBILE_REVIEWER,
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
