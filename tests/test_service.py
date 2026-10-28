import pytest

import qa_orchestrator.orchestration as orchestration_module
from qa_orchestrator.config import Settings
from qa_orchestrator.contracts import ReviewAgent, ReviewBundle
from qa_orchestrator.orchestration import (
    AdvanceQaOrchestrationRequest,
    DeepReviewSignals,
    OrchestrationStatus,
    OrchestrationStep,
)
from qa_orchestrator.review_profiles import REVIEW_BUNDLES, bundle_execution_groups
from qa_orchestrator.service import OrchestratorService


@pytest.mark.parametrize("profile", [ReviewAgent.PR_TEST_ANALYZER, ReviewAgent.CODE_EXPLORER])
def test_implementation_review_requires_acknowledging_limited_profile(tmp_path, profile):
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    started = service.start_qa_orchestration("ordinary_review")
    request = AdvanceQaOrchestrationRequest(
        run_id=started.run_id, completed_step="triage", status="completed",
        selected_profile=profile, implementation_review=True,
    )
    with pytest.raises(ValueError, match="cannot report implementation findings"):
        service.advance_qa_orchestration(request)
    assert service.get_qa_orchestration(started.run_id).current_step == "triage"
    accepted = service.advance_qa_orchestration(
        request.model_copy(update={"acknowledge_limited_profile": True})
    )
    assert accepted.current_step == "primary_review"
    assert "cannot report implementation findings" in accepted.route_warning


def test_limited_profile_warns_legacy_client_but_full_profile_does_not(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    for profile, warned in [(ReviewAgent.PR_TEST_ANALYZER, True), (ReviewAgent.CODE_REVIEWER, False)]:
        started = service.start_qa_orchestration("ordinary_review")
        result = service.advance_qa_orchestration(AdvanceQaOrchestrationRequest(
            run_id=started.run_id, completed_step="triage", status="completed", selected_profile=profile
        ))
        assert bool(result.route_warning) is warned


def test_implementation_review_can_select_bundle_after_warning(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    started = service.start_qa_orchestration("ordinary_review")
    limited = AdvanceQaOrchestrationRequest(
        run_id=started.run_id, completed_step="triage", status="completed",
        selected_profile=ReviewAgent.PR_TEST_ANALYZER, implementation_review=True,
    )
    with pytest.raises(ValueError, match="cannot report implementation findings"):
        service.advance_qa_orchestration(limited)
    selected = service.advance_qa_orchestration(AdvanceQaOrchestrationRequest(
        run_id=started.run_id, completed_step="triage", status="completed",
        selected_bundle=ReviewBundle.ORDINARY_MR, implementation_review=True,
    ))
    assert selected.selected_bundle == ReviewBundle.ORDINARY_MR
    assert selected.route_warning is None


def test_triage_intent_is_part_of_retry_identity(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    started = service.start_qa_orchestration("other")
    request = AdvanceQaOrchestrationRequest(
        run_id=started.run_id, completed_step="triage", status="completed",
        selected_profile=ReviewAgent.PR_TEST_ANALYZER, request_id="triage-1", expected_revision=0,
        implementation_review=False,
    )
    first = service.advance_qa_orchestration(request)
    assert service.advance_qa_orchestration(request).revision == first.revision
    with pytest.raises(ValueError, match="request_id reused with a different transition"):
        service.advance_qa_orchestration(request.model_copy(update={"implementation_review": None}))


def _ready_for_host_outcome(service: OrchestratorService, task_type: str = "ordinary_review"):
    started = service.start_qa_orchestration(task_type)
    primary = service.advance_qa_orchestration(
        AdvanceQaOrchestrationRequest(
            run_id=started.run_id,
            completed_step=OrchestrationStep.TRIAGE,
            status="completed",
            selected_profile=ReviewAgent.CODE_REVIEWER,
        )
    )
    synthesis = service.advance_qa_orchestration(
        AdvanceQaOrchestrationRequest(
            run_id=started.run_id,
            completed_step=primary.current_step,
            status="completed",
            completed_profile=ReviewAgent.CODE_REVIEWER,
            risk_signals=DeepReviewSignals(),
        )
    )
    awaiting_outcome = service.advance_qa_orchestration(
        AdvanceQaOrchestrationRequest(
            run_id=started.run_id,
            completed_step=synthesis.current_step,
            status="completed",
        )
    )
    assert awaiting_outcome.status is OrchestrationStatus.AWAITING_HOST_OUTCOME
    return started


def test_service_resolves_all_review_profiles(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    routes = [service.prepare_review_route(profile) for profile in ReviewAgent]

    assert [route.profile for route in routes] == list(ReviewAgent)
    assert all(route.read_only and route.host_owns_decisions for route in routes)


def test_service_exposes_fixed_profiles_and_bundles(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    session = service.start_qa_orchestration("ordinary_review")

    assert session.allowed_profiles == list(ReviewAgent)
    assert session.allowed_bundles == list(ReviewBundle)
    assert session.recommended_bundles == [ReviewBundle.ORDINARY_MR]
    assert session.selected_bundle is None
    assert session.review_profiles == []
    assert session.model_policy.mode == "host_default"
    assert service.prepare_review_route(ReviewAgent.CODE_EXPLORER).display_name == (
        "Faraday — Evidence Investigator"
    )


def test_review_profiles_require_evidence_backed_impact_tracing(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    constraints = service.prepare_review_route(ReviewAgent.CODE_EXPLORER).constraints
    combined = " ".join(constraints).lower()

    assert "inbound callers" in combined
    assert "external consumer" in combined
    assert "no matches do not prove no dependency" in combined
    assert "source locations covered by cited head-source evidence" in combined






def test_service_advances_with_a_fixed_bundle(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    started = service.start_qa_orchestration("ordinary_review")

    advanced = service.advance_qa_orchestration(
        AdvanceQaOrchestrationRequest(
            run_id=started.run_id,
            completed_step=OrchestrationStep.TRIAGE,
            status="completed",
            selected_bundle=ReviewBundle.ORDINARY_MR,
        )
    )

    assert advanced.selected_bundle is ReviewBundle.ORDINARY_MR
    assert advanced.review_profiles == list(REVIEW_BUNDLES[ReviewBundle.ORDINARY_MR])
    assert [route.profile for route in advanced.selected_review_routes.routes] == list(
        REVIEW_BUNDLES[ReviewBundle.ORDINARY_MR]
    )
    assert advanced.selected_review_routes.execution_groups == [
        list(group) for group in bundle_execution_groups(ReviewBundle.ORDINARY_MR)
    ]


def test_service_returns_single_profile_checklist_with_triage_selection(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    started = service.start_qa_orchestration("ordinary_review")

    advanced = service.advance_qa_orchestration(
        AdvanceQaOrchestrationRequest(
            run_id=started.run_id,
            completed_step=OrchestrationStep.TRIAGE,
            status="completed",
            selected_profile=ReviewAgent.PYTHON_REVIEWER,
        )
    )

    assert [route.profile for route in advanced.selected_review_routes.routes] == [
        ReviewAgent.PYTHON_REVIEWER
    ]
    assert advanced.selected_review_routes.execution_groups == [[ReviewAgent.PYTHON_REVIEWER]]
    assert not hasattr(service.get_qa_orchestration(started.run_id), "selected_review_routes")


def test_service_keeps_single_profile_for_implementation_review(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    started = service.start_qa_orchestration("ordinary_review")

    advanced = service.advance_qa_orchestration(
        AdvanceQaOrchestrationRequest(
            run_id=started.run_id,
            completed_step=OrchestrationStep.TRIAGE,
            status="completed",
            selected_profile=ReviewAgent.PYTHON_REVIEWER,
            implementation_review=True,
        )
    )

    expected_profiles = [ReviewAgent.PYTHON_REVIEWER]
    assert advanced.review_profiles == expected_profiles
    assert [route.profile for route in advanced.selected_review_routes.routes] == expected_profiles
    assert advanced.selected_review_routes.execution_groups == [
        [ReviewAgent.PYTHON_REVIEWER],
    ]


def test_service_replay_returns_routes_persisted_by_older_triage_resolver(tmp_path, monkeypatch):
    settings = Settings(
        data_dir=tmp_path,
        orchestration_session_store_path=tmp_path / "sessions.sqlite3",
    )
    service = OrchestratorService(settings)
    started = service.start_qa_orchestration("ordinary_review")
    request = AdvanceQaOrchestrationRequest(
        run_id=started.run_id,
        completed_step=OrchestrationStep.TRIAGE,
        status="completed",
        selected_profile=ReviewAgent.PYTHON_REVIEWER,
        implementation_review=True,
        request_id="triage-old-resolver",
        expected_revision=0,
    )

    with monkeypatch.context() as old_resolver:
        old_resolver.setattr(
            orchestration_module,
            "triage_review_profiles",
            lambda **_kwargs: [ReviewAgent.PYTHON_REVIEWER],
        )
        first = service.advance_qa_orchestration(request)

    assert first.review_profiles == [ReviewAgent.PYTHON_REVIEWER]

    restored_service = OrchestratorService(settings)
    restored = restored_service.get_qa_orchestration(started.run_id)
    replay = restored_service.advance_qa_orchestration(request)

    assert replay.review_profiles == [ReviewAgent.PYTHON_REVIEWER]
    assert replay.revision == restored.revision == first.revision
    assert [route.profile for route in replay.selected_review_routes.routes] == replay.review_profiles
    assert replay.selected_review_routes.execution_groups == [[ReviewAgent.PYTHON_REVIEWER]]


@pytest.mark.parametrize("implementation_review", [None, False])
@pytest.mark.parametrize(
    "profile",
    [
        ReviewAgent.CODE_REVIEWER,
        ReviewAgent.PR_TEST_ANALYZER,
        ReviewAgent.CODE_EXPLORER,
    ],
)
def test_service_preserves_non_implementation_review_single_profile_routes(
    tmp_path, profile, implementation_review
):
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    started = service.start_qa_orchestration("ordinary_review")

    advanced = service.advance_qa_orchestration(
        AdvanceQaOrchestrationRequest(
            run_id=started.run_id,
            completed_step=OrchestrationStep.TRIAGE,
            status="completed",
            selected_profile=profile,
            implementation_review=implementation_review,
        )
    )

    assert advanced.review_profiles == [profile]
    assert [route.profile for route in advanced.selected_review_routes.routes] == [profile]
    assert advanced.selected_review_routes.execution_groups == [[profile]]


@pytest.mark.parametrize(
    "profile,acknowledge",
    [
        (ReviewAgent.PR_TEST_ANALYZER, True),
        (ReviewAgent.CODE_EXPLORER, True),
    ],
)
def test_service_does_not_duplicate_limited_or_auditor_profiles(
    tmp_path, profile, acknowledge
):
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    started = service.start_qa_orchestration("ordinary_review")

    advanced = service.advance_qa_orchestration(
        AdvanceQaOrchestrationRequest(
            run_id=started.run_id,
            completed_step=OrchestrationStep.TRIAGE,
            status="completed",
            selected_profile=profile,
            implementation_review=True,
            acknowledge_limited_profile=acknowledge,
        )
    )

    assert advanced.review_profiles == [profile]
    assert [route.profile for route in advanced.selected_review_routes.routes] == [profile]


def test_service_prepares_triage_routes_before_advancing_session(tmp_path, monkeypatch):
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    started = service.start_qa_orchestration("ordinary_review")

    def fail_route_preparation(bundle):
        raise ValueError("route definitions are invalid")

    monkeypatch.setattr(service, "prepare_review_bundle", fail_route_preparation)
    with pytest.raises(ValueError, match="route definitions are invalid"):
        service.advance_qa_orchestration(
            AdvanceQaOrchestrationRequest(
                run_id=started.run_id,
                completed_step=OrchestrationStep.TRIAGE,
                status="completed",
                selected_bundle=ReviewBundle.ORDINARY_MR,
            )
        )

    assert service.get_qa_orchestration(started.run_id).current_step is OrchestrationStep.TRIAGE



def test_service_keeps_host_default_policy_across_stage_transitions(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    started = service.start_qa_orchestration("ordinary_review")

    primary = service.advance_qa_orchestration(
        AdvanceQaOrchestrationRequest(
            run_id=started.run_id,
            completed_step=OrchestrationStep.TRIAGE,
            status="completed",
            selected_profile=ReviewAgent.CODE_REVIEWER,
        )
    )
    synthesis = service.advance_qa_orchestration(
        AdvanceQaOrchestrationRequest(
            run_id=started.run_id,
            completed_step=primary.current_step,
            status="completed",
            completed_profile=ReviewAgent.CODE_REVIEWER,
            risk_signals=DeepReviewSignals(),
        )
    )

    assert started.model_policy.mode == "host_default"
    assert primary.model_policy.mode == "host_default"
    assert synthesis.current_step is OrchestrationStep.SYNTHESIS
    assert synthesis.model_policy.mode == "host_default"


def test_service_rejects_unknown_review_profile(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    with pytest.raises(ValueError, match="unknown review agent profile"):
        service.prepare_review_route("not_a_profile")


@pytest.mark.parametrize(
    ("task_type", "recommended"),
    [
        ("widget_review", [ReviewBundle.WIDGET, ReviewBundle.WIDGET_JS]),
        ("epic_analysis", [ReviewBundle.REQUIREMENTS]),
        ("requirements_analysis", [ReviewBundle.REQUIREMENTS]),
        ("qa_planning", [ReviewBundle.REQUIREMENTS]),
        ("autotest_implementation", [ReviewBundle.AUTOTEST, ReviewBundle.ORDINARY_MR]),
        ("other", []),
    ],
)
def test_service_recommends_bundles_without_restricting_choices(tmp_path, task_type, recommended):
    session = OrchestratorService.from_settings(data_dir=tmp_path).start_qa_orchestration(task_type)

    assert session.recommended_bundles == recommended
    assert session.allowed_bundles == list(ReviewBundle)


def test_service_finishes_orchestrated_session_without_persisting_task_data(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    started = _ready_for_host_outcome(service)

    final = service.finish_qa_orchestration(run_id=started.run_id, outcome="completed")

    assert final.status is OrchestrationStatus.COMPLETED
    assert final.next_action == "Host recorded the final QA outcome."
    assert list(tmp_path.iterdir()) == []


def test_service_restores_unfinished_session_when_store_is_configured(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        orchestration_session_store_path=tmp_path / "sessions.sqlite3",
    )
    started = OrchestratorService(settings).start_qa_orchestration("ordinary_review")

    restored = OrchestratorService(settings).get_qa_orchestration(started.run_id)

    assert restored.model_dump() == started.model_dump()


@pytest.mark.parametrize("outcome", ["partial", "blocked"])
def test_service_finishes_a_stopped_orchestration(tmp_path, outcome):
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    started = service.start_qa_orchestration("ordinary_review")
    stopped = service.advance_qa_orchestration(
        AdvanceQaOrchestrationRequest(
            run_id=started.run_id,
            completed_step=OrchestrationStep.TRIAGE,
            status=outcome,
        )
    )

    final = service.finish_qa_orchestration(run_id=started.run_id, outcome=outcome)

    assert final.status.value == outcome
    assert "recorded" in final.next_action
    assert final.next_action != stopped.next_action


def test_service_finishing_same_outcome_is_idempotent(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    started = _ready_for_host_outcome(service)

    first = service.finish_qa_orchestration(run_id=started.run_id, outcome="completed")
    repeated = service.finish_qa_orchestration(run_id=started.run_id, outcome="completed")

    assert repeated.model_dump() == first.model_dump()


def test_service_rejects_a_conflicting_finalized_outcome(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    started = _ready_for_host_outcome(service)
    service.finish_qa_orchestration(run_id=started.run_id, outcome="completed")

    with pytest.raises(ValueError, match="conflicting final outcome"):
        service.finish_qa_orchestration(run_id=started.run_id, outcome="partial")

    retained = service.get_qa_orchestration(started.run_id)
    assert retained.status.value == "completed"


def test_removed_auditor_is_not_an_accepted_review_profile():
    from pydantic import TypeAdapter, ValidationError

    with pytest.raises(ValidationError):
        TypeAdapter(ReviewAgent).validate_python("omission_auditor")
