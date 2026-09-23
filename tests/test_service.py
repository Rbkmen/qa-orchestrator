import json

import pytest

from qa_orchestrator.contracts import ReviewAgent, ReviewBundle
from qa_orchestrator.events import DISTRIBUTION_EVENT_TYPE, DISTRIBUTION_SCHEMA_VERSION
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


def test_service_records_only_task_type_for_distribution(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    receipt = service.record_qa_task_outcome(
        task_type="ordinary_review",
        outcome="completed",
    )

    assert receipt.status == "recorded"
    event = json.loads((tmp_path / "metrics.jsonl").read_text(encoding="utf-8"))
    assert set(event) == {"schema_version", "event_type", "timestamp", "task_type"}
    assert event["schema_version"] == DISTRIBUTION_SCHEMA_VERSION
    assert event["event_type"] == DISTRIBUTION_EVENT_TYPE
    assert event["task_type"] == "ordinary_review"


@pytest.mark.parametrize("outcome", ["completed", "partial", "blocked"])
def test_service_records_unorchestrated_task_categories_for_any_final_status(tmp_path, outcome):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    receipt = service.record_qa_task_outcome(
        task_type="qa_planning",
        outcome=outcome,
    )

    assert receipt.status == "recorded"
    event = json.loads((tmp_path / "metrics.jsonl").read_text(encoding="utf-8"))
    assert event["task_type"] == "qa_planning"
    assert "outcome" not in event


@pytest.mark.parametrize("task_type", ["unknown", [], {}])
def test_service_rejects_unknown_task_types(tmp_path, task_type):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    with pytest.raises(ValueError, match="unknown QA task type"):
        service.record_qa_task_outcome(task_type=task_type, outcome="completed")


def test_service_rejects_unknown_final_outcomes(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    with pytest.raises(ValueError, match="invalid QA task outcome"):
        service.record_qa_task_outcome(task_type="ordinary_review", outcome="unknown")


def test_service_records_orchestrated_task_and_finalizes_session(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    started = _ready_for_host_outcome(service)

    receipt = service.record_qa_task_outcome(
        task_type="ordinary_review",
        outcome="completed",
        run_id=started.run_id,
    )

    assert receipt.status == "recorded"
    session = service.get_qa_orchestration(started.run_id)
    assert session.status is OrchestrationStatus.COMPLETED
    assert session.outcome_recorded is True
    event = json.loads((tmp_path / "metrics.jsonl").read_text(encoding="utf-8"))
    assert set(event) == {"schema_version", "event_type", "timestamp", "task_type"}


@pytest.mark.parametrize("outcome", ["partial", "blocked"])
def test_service_records_a_stopped_orchestration(tmp_path, outcome):
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    started = service.start_qa_orchestration("ordinary_review")
    stopped = service.advance_qa_orchestration(
        AdvanceQaOrchestrationRequest(
            run_id=started.run_id,
            completed_step=OrchestrationStep.TRIAGE,
            status=outcome,
        )
    )

    receipt = service.record_qa_task_outcome(
        task_type="ordinary_review",
        outcome=outcome,
        run_id=started.run_id,
    )

    assert receipt.status == "recorded"
    final = service.get_qa_orchestration(started.run_id)
    assert final.outcome_recorded is True
    assert final.status.value == outcome
    assert "recorded" in final.next_action
    assert final.next_action != stopped.next_action


def test_service_rejects_outcome_with_mismatched_task_type(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    started = _ready_for_host_outcome(service)

    with pytest.raises(ValueError, match="task_type"):
        service.record_qa_task_outcome(
            task_type="widget_review",
            outcome="completed",
            run_id=started.run_id,
        )

    assert service.get_qa_orchestration(started.run_id).status is (
        OrchestrationStatus.AWAITING_HOST_OUTCOME
    )
    assert not (tmp_path / "metrics.jsonl").exists()


def test_service_does_not_duplicate_finalized_task_distribution(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    started = _ready_for_host_outcome(service)
    call = {
        "task_type": "ordinary_review",
        "outcome": "completed",
        "run_id": started.run_id,
    }

    assert service.record_qa_task_outcome(**call).status == "recorded"
    assert service.record_qa_task_outcome(**call).status == "recorded"

    lines = (tmp_path / "metrics.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


def test_service_rejects_a_conflicting_finalized_outcome(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    started = _ready_for_host_outcome(service)
    service.record_qa_task_outcome(
        task_type="ordinary_review",
        outcome="completed",
        run_id=started.run_id,
    )

    with pytest.raises(ValueError, match="conflicting final outcome"):
        service.record_qa_task_outcome(
            task_type="ordinary_review",
            outcome="partial",
            run_id=started.run_id,
        )
