import pytest
from pydantic import ValidationError

from qa_orchestrator.contracts import ReviewAgent, ReviewContext, ReviewRoute


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


def test_prior_review_decisions_are_bound_to_same_mr_and_ancestor_head(bound_review_context):
    payload = bound_review_context.model_dump(mode="python")
    payload["evidence_refs"] = ("E1", "E2", "H1")
    payload["evidence_registry"] = (
        {
            "evidence_ref": "E1", "kind": "other", "sha256": "a" * 64,
            "locator": "https://gitlab.example/group/service/-/merge_requests/123",
        },
        {
            "evidence_ref": "E2", "kind": "other", "sha256": "b" * 64,
            "locator": "https://gitlab.example/group/service/-/merge_requests/123",
        },
        {
            "evidence_ref": "H1", "kind": "prior_review", "sha256": "c" * 64,
            "revision": "5" * 40,
            "locator": "https://gitlab.example/group/service/-/merge_requests/123#note_9",
        },
    )
    payload["current_merge_request_source_id"] = "MR-123"
    payload["prior_review_decisions"] = ({
        "merge_request_source_id": "MR-123",
        "reviewed_head_sha": "5" * 40,
        "revision_relation": "verified_ancestor",
        "candidate_id": "F-09",
        "disposition": "resolved",
        "summary": "Fixed in the follow-up commit and covered by a regression test.",
        "evidence_ref": "H1",
    },)

    context = ReviewContext.model_validate(payload)

    assert context.prior_review_decisions[0].candidate_id == "F-09"
    assert context.model_dump(mode="json")["prior_review_decisions"][0]["evidence_ref"] == "H1"


def test_review_context_omits_empty_prior_history(bound_review_context):
    assert "prior_review_decisions" not in bound_review_context.model_dump(mode="json")


def test_prior_review_decisions_reject_different_mr(bound_review_context):
    payload = bound_review_context.model_dump(mode="python")
    payload["evidence_refs"] = ("E1", "E2", "H1")
    payload["evidence_registry"] = (
        {
            "evidence_ref": "E1", "kind": "other", "sha256": "a" * 64,
            "locator": "https://gitlab.example/group/service/-/merge_requests/123",
        },
        {
            "evidence_ref": "E2", "kind": "other", "sha256": "b" * 64,
            "locator": "https://gitlab.example/group/service/-/merge_requests/123",
        },
        {
            "evidence_ref": "H1", "kind": "prior_review", "sha256": "c" * 64,
            "revision": "5" * 40,
            "locator": "https://gitlab.example/group/service/-/merge_requests/123#note_9",
        },
    )
    payload["prior_review_decisions"] = ({
        "merge_request_source_id": "MR-OTHER",
        "reviewed_head_sha": "5" * 40,
        "revision_relation": "verified_ancestor",
        "candidate_id": "F-09",
        "disposition": "resolved",
        "summary": "Previously marked fixed.",
        "evidence_ref": "H1",
    },)

    payload["current_merge_request_source_id"] = "MR-123"
    with pytest.raises(ValidationError, match="current merge request source"):
        ReviewContext.model_validate(payload)


def test_prior_review_decisions_reject_another_declared_merge_request(bound_review_context):
    payload = bound_review_context.model_dump(mode="python")
    payload["input_manifest"] = {
        "sources": (
            *payload["input_manifest"]["sources"],
            {
                "source_id": "MR-456",
                "kind": "merge_request",
                "locator": "https://gitlab.example/group/service/-/merge_requests/456",
                "required": True,
                "retrieval_status": "complete",
                "pagination_status": "not_applicable",
                "extraction_status": "complete",
                "criterion_refs": (),
            },
        )
    }
    payload["current_merge_request_source_id"] = "MR-123"
    payload["evidence_refs"] = ("E1", "E2", "H1")
    payload["evidence_registry"] = (
        {
            "evidence_ref": "E1", "kind": "other", "sha256": "a" * 64,
            "locator": "https://gitlab.example/group/service/-/merge_requests/123",
        },
        {
            "evidence_ref": "E2", "kind": "other", "sha256": "b" * 64,
            "locator": "https://gitlab.example/group/service/-/merge_requests/123",
        },
        {
            "evidence_ref": "H1", "kind": "prior_review", "sha256": "c" * 64,
            "revision": "5" * 40,
            "locator": "https://gitlab.example/group/service/-/merge_requests/456#note_9",
        },
    )
    payload["prior_review_decisions"] = ({
        "merge_request_source_id": "MR-456",
        "reviewed_head_sha": "5" * 40,
        "revision_relation": "verified_ancestor",
        "candidate_id": "F-09",
        "disposition": "resolved",
        "summary": "Previously marked fixed.",
        "evidence_ref": "H1",
    },)

    with pytest.raises(ValidationError, match="must target the current merge request source"):
        ReviewContext.model_validate(payload)


def test_prior_review_ref_rejects_mr_locator_prefix_collision(bound_review_context):
    payload = bound_review_context.model_dump(mode="python")
    payload["evidence_refs"] = ("E1", "E2", "H1")
    payload["evidence_registry"] = (
        {
            "evidence_ref": "E1", "kind": "other", "sha256": "a" * 64,
            "locator": "https://gitlab.example/group/service/-/merge_requests/123",
        },
        {
            "evidence_ref": "E2", "kind": "other", "sha256": "b" * 64,
            "locator": "https://gitlab.example/group/service/-/merge_requests/123",
        },
        {
            "evidence_ref": "H1", "kind": "prior_review", "sha256": "c" * 64,
            "revision": "5" * 40,
            "locator": "https://gitlab.example/group/service/-/merge_requests/1234#note_9",
        },
    )
    payload["current_merge_request_source_id"] = "MR-123"
    payload["prior_review_decisions"] = ({
        "merge_request_source_id": "MR-123",
        "reviewed_head_sha": "5" * 40,
        "revision_relation": "verified_ancestor",
        "candidate_id": "F-09",
        "disposition": "resolved",
        "summary": "Previously marked fixed.",
        "evidence_ref": "H1",
    },)

    with pytest.raises(ValidationError, match="same merge request and head"):
        ReviewContext.model_validate(payload)


def test_prior_review_decisions_reject_unverified_head_relation(bound_review_context):
    payload = bound_review_context.model_dump(mode="python")
    payload["evidence_refs"] = ("E1", "E2", "H1")
    payload["evidence_registry"] = (
        {
            "evidence_ref": "E1", "kind": "other", "sha256": "a" * 64,
            "locator": "https://gitlab.example/group/service/-/merge_requests/123",
        },
        {
            "evidence_ref": "E2", "kind": "other", "sha256": "b" * 64,
            "locator": "https://gitlab.example/group/service/-/merge_requests/123",
        },
        {
            "evidence_ref": "H1", "kind": "prior_review", "sha256": "c" * 64,
            "revision": "5" * 40,
            "locator": "https://gitlab.example/group/service/-/merge_requests/123#note_9",
        },
    )
    payload["current_merge_request_source_id"] = "MR-123"
    payload["prior_review_decisions"] = ({
        "merge_request_source_id": "MR-123",
        "reviewed_head_sha": "5" * 40,
        "revision_relation": "same_head",
        "candidate_id": "F-09",
        "disposition": "resolved",
        "summary": "Previously marked fixed.",
        "evidence_ref": "H1",
    },)

    with pytest.raises(ValidationError, match="current head or a verified ancestor"):
        ReviewContext.model_validate(payload)
