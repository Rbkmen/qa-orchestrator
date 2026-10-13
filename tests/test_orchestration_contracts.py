from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from qa_orchestrator.contracts import (
    FinalReviewReport,
    ProfileExecutionMetrics,
    ProfileReviewReport,
    ReviewAgent,
    ReviewBundle,
    ReviewContext,
    ReviewFindingLocation,
    ReviewNote,
    ReviewRouteSelection,
)
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
from qa_orchestrator.review_profiles import build_review_route


def test_review_route_selection_rejects_group_order_mismatch():
    routes = [
        build_review_route(ReviewAgent.CODE_EXPLORER),
        build_review_route(ReviewAgent.CODE_REVIEWER),
    ]

    with pytest.raises(ValidationError, match="execution_groups must match route profiles"):
        ReviewRouteSelection(
            routes=routes,
            execution_groups=[[ReviewAgent.CODE_REVIEWER], [ReviewAgent.CODE_EXPLORER]],
        )


@pytest.mark.parametrize(
    ("routes", "execution_groups"),
    [
        (
            [build_review_route(ReviewAgent.CODE_EXPLORER)],
            [[], [ReviewAgent.CODE_EXPLORER]],
        ),
        (
            [build_review_route(ReviewAgent.CODE_EXPLORER)] * 2,
            [[ReviewAgent.CODE_EXPLORER, ReviewAgent.CODE_EXPLORER]],
        ),
    ],
)
def test_review_route_selection_rejects_empty_groups_and_duplicate_profiles(
    routes, execution_groups
):
    with pytest.raises(ValidationError, match="execution_groups must match route profiles"):
        ReviewRouteSelection(routes=routes, execution_groups=execution_groups)


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
    assert set(policy.model_dump(mode="json")) == {
        "mode",
        "provider",
        "model",
        "reasoning",
        "reasoning_capabilities_verified",
    }
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


def test_review_context_requires_full_unique_review_identity(bound_review_context):
    assert bound_review_context.base_sha == "0" * 40
    assert bound_review_context.diff_sha256 == "2" * 64

    with pytest.raises(ValidationError, match="different revisions"):
        ReviewContext(**{**bound_review_context.model_dump(), "head_sha": "0" * 40})

    with pytest.raises(ValidationError):
        ReviewContext(**{**bound_review_context.model_dump(), "diff_sha256": "not-a-digest"})

    with pytest.raises(ValidationError, match="evidence_refs must be unique"):
        ReviewContext(**{**bound_review_context.model_dump(), "evidence_refs": ("E1", "E1")})


def test_review_context_bounds_paths_and_rejects_repository_credentials(bound_review_context):
    context = bound_review_context.model_dump()
    assert ReviewContext(**{**context, "changed_files": ("x" * 1024,)}).changed_files == (
        "x" * 1024,
    )

    with pytest.raises(ValidationError):
        ReviewContext(**{**context, "changed_files": ("x" * 1025,)})

    for repository in (
        "https://user:secret@github.com/group/repo",
        "https://github.com/group/repo?token=secret",
        "github.com/group/repo?token=secret",
        "token@github.com/group/repo",
        "git@github.com:group/repo.git",
    ):
        with pytest.raises(ValidationError, match="credentials|parameters|clone|query or fragment"):
            ReviewContext(**{**context, "repository": repository})


def test_finding_locations_require_a_valid_line_range():
    assert ReviewFindingLocation(path="src/service.py", start_line=10, end_line=12)

    with pytest.raises(ValidationError, match="end_line must be greater than or equal"):
        ReviewFindingLocation(path="src/service.py", start_line=12, end_line=10)

    with pytest.raises(ValidationError):
        ReviewFindingLocation(path="src/service.py", start_line=0)


def test_profile_execution_metrics_require_an_ordered_aware_interval():
    started_at = datetime.now(UTC)
    metrics = ProfileExecutionMetrics(
        started_at=started_at,
        completed_at=started_at + timedelta(seconds=2),
        input_tokens=120,
        output_tokens=45,
    )
    assert metrics.output_tokens == 45

    with pytest.raises(ValidationError, match="completed_at must be at or after"):
        ProfileExecutionMetrics(
            started_at=started_at,
            completed_at=started_at - timedelta(seconds=1),
        )

    with pytest.raises(ValidationError):
        ProfileExecutionMetrics(
            started_at=started_at,
            completed_at=started_at + timedelta(seconds=1),
            input_tokens=-1,
        )


def test_profile_and_final_reports_have_serialized_size_limits():
    large_notes = tuple(ReviewNote(text="x" * 2000) for _ in range(40))

    with pytest.raises(ValidationError, match="profile report exceeds"):
        ProfileReviewReport(
            profile=ReviewAgent.CODE_REVIEWER,
            scope="Reviewed the change.",
            coverage_gaps=large_notes,
        )

    with pytest.raises(ValidationError, match="final review report exceeds"):
        FinalReviewReport(
            summary="Synthesis completed.",
            profiles_reviewed=(ReviewAgent.CODE_REVIEWER,),
            unverified=large_notes,
        )


def test_session_has_an_aggregate_review_artifact_size_limit(bound_review_context):
    notes = tuple(ReviewNote(text="x" * 2000) for _ in range(26))
    profiles = tuple(ReviewAgent)
    reports = [
        ProfileReviewReport(profile=profile, scope="Reviewed the change.", coverage_gaps=notes)
        for profile in profiles
    ]

    with pytest.raises(ValidationError, match="session review artifacts exceed"):
        QaOrchestrationSession(
            run_id="qar-0123456789abcdef0123456789abcdef",
            task_type="ordinary_review",
            status=OrchestrationStatus.ACTIVE,
            current_step=OrchestrationStep.PRIMARY_REVIEW,
            completed_profiles=list(profiles),
            profile_reports=reports,
            review_context=bound_review_context,
            next_action="Host continues review.",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )


def test_enabled_risk_signals_require_rationale_and_evidence_refs():
    with pytest.raises(ValidationError, match="enabled risk signals require"):
        DeepReviewSignals(cross_system_scope=True)

    signals = DeepReviewSignals(
        cross_system_scope=True,
        rationale="The changed contract crosses two services.",
        evidence_refs=("E1",),
    )
    assert signals.cross_system_scope is True

    with pytest.raises(ValidationError, match="risk signal evidence_refs must be unique"):
        DeepReviewSignals(
            evidence_conflict=True,
            rationale="Two authoritative contracts disagree.",
            evidence_refs=("E1", "E1"),
        )


def test_profile_reports_keep_explorer_and_test_analyzer_within_their_scope(
    review_finding_candidate,
):
    for profile in (ReviewAgent.CODE_EXPLORER, ReviewAgent.PR_TEST_ANALYZER):
        with pytest.raises(ValidationError, match="cannot report implementation findings"):
            ProfileReviewReport(
                profile=profile,
                scope="Changed code and tests.",
                finding_candidates=(review_finding_candidate,),
            )


def test_advance_request_requires_profile_reports_in_completion_order(
    make_profile_review_report,
):
    with pytest.raises(ValidationError, match="profile_reports must match completed profiles"):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.PRIMARY_REVIEW,
            status="completed",
            completed_profiles=[ReviewAgent.CODE_REVIEWER, ReviewAgent.PR_TEST_ANALYZER],
            profile_reports=[
                make_profile_review_report(ReviewAgent.PR_TEST_ANALYZER),
                make_profile_review_report(ReviewAgent.CODE_REVIEWER),
            ],
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
    signals = DeepReviewSignals(
        high_risk_domain=True,
        evidence_uncertain=True,
        rationale="A permission conclusion remains uncertain.",
        evidence_refs=("E1",),
    )

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
            risk_signals=DeepReviewSignals(
                cross_system_scope=True,
                rationale="The changed behavior crosses services.",
                evidence_refs=("E1",),
            ),
        )


def test_skip_synthesis_is_only_valid_after_a_completed_primary_review():
    with pytest.raises(ValidationError, match="skip_synthesis"):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.TRIAGE,
            status="completed",
            selected_profile=ReviewAgent.CODE_REVIEWER,
            skip_synthesis=True,
        )


def test_completed_primary_review_requires_completed_profile():
    with pytest.raises(ValidationError, match="completed_profile"):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.PRIMARY_REVIEW,
            status="completed",
        )


def test_completed_primary_review_accepts_a_group_of_completed_profiles():
    request = AdvanceQaOrchestrationRequest(
        run_id="qar-0123456789abcdef0123456789abcdef",
        completed_step=OrchestrationStep.PRIMARY_REVIEW,
        status="completed",
        completed_profiles=[ReviewAgent.CODE_REVIEWER, ReviewAgent.PR_TEST_ANALYZER],
        risk_signals=DeepReviewSignals(),
    )

    assert request.completed_profiles == [
        ReviewAgent.CODE_REVIEWER,
        ReviewAgent.PR_TEST_ANALYZER,
    ]


def test_completed_profiles_must_be_nonempty_and_exclusive_with_legacy_field():
    base = {
        "run_id": "qar-0123456789abcdef0123456789abcdef",
        "completed_step": OrchestrationStep.PRIMARY_REVIEW,
        "status": "completed",
    }

    with pytest.raises(ValidationError, match="at least one"):
        AdvanceQaOrchestrationRequest(**base, completed_profiles=[])

    with pytest.raises(ValidationError, match="mutually exclusive"):
        AdvanceQaOrchestrationRequest(
            **base,
            completed_profile=ReviewAgent.CODE_REVIEWER,
            completed_profiles=[ReviewAgent.CODE_REVIEWER],
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
