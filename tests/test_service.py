import pytest

from qa_orchestrator.contracts import ReviewAgent, ReviewBundle
from qa_orchestrator.orchestration import (
    AdvanceQaOrchestrationRequest,
    DeepReviewSignals,
    OrchestrationStatus,
    OrchestrationStep,
)
from qa_orchestrator.review_profiles import REVIEW_BUNDLES
from qa_orchestrator.service import OrchestratorService


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
    assert session.selected_bundle is None
    assert session.review_profiles == []
    assert session.model_policy.model.value == "gpt-6-luna"
    assert session.model_policy.reasoning == "max"
    assert service.prepare_review_route(ReviewAgent.CODE_EXPLORER).display_name == (
        "Faraday — Evidence Investigator"
    )


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


def test_service_rejects_unknown_review_profile(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    with pytest.raises(ValueError, match="unknown review agent profile"):
        service.prepare_review_route("not_a_profile")


def test_service_finishes_orchestrated_session_without_persisting_task_data(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    started = _ready_for_host_outcome(service)

    final = service.finish_qa_orchestration(run_id=started.run_id, outcome="completed")

    assert final.status is OrchestrationStatus.COMPLETED
    assert final.next_action == "Host recorded the final QA outcome."
    assert list(tmp_path.iterdir()) == []


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
