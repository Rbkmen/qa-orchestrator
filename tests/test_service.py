import json

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
    assert session.model_policy.model.value == "gpt-5.6-luna"
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
            completed_step=OrchestrationStep.LUNA_TRIAGE,
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


def test_service_records_content_free_task_outcome(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    receipt = service.record_qa_task_outcome(
        task_type="ordinary_review",
        outcome="completed",
        codegraph_calls=1,
        source_mcp_calls=2,
        findings_identified=3,
        findings_confirmed=2,
        findings_rejected=1,
        repeated_source_reads=1,
        deep_analysis_used=True,
        deep_model="gpt-5.6-sol",
        deep_reasoning="high",
        deep_duration_ms=1200,
    )

    assert receipt.status == "recorded"
    event = json.loads((tmp_path / "metrics.jsonl").read_text(encoding="utf-8"))
    assert event["event_type"] == "qa_task_outcome"
    assert event["deep_model"] == "gpt-5.6-sol"
    assert "legacy_metric" not in event


def test_service_rejects_inconsistent_task_outcome(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    with pytest.raises(ValueError, match="QA task metrics are inconsistent"):
        service.record_qa_task_outcome(
            task_type="ordinary_review",
            outcome="completed",
            codegraph_calls=0,
            source_mcp_calls=0,
            findings_identified=1,
            findings_confirmed=2,
            findings_rejected=0,
            repeated_source_reads=0,
        )


def test_service_rejects_unapproved_deep_model(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    with pytest.raises(ValueError, match="QA task metrics are inconsistent"):
        service.record_qa_task_outcome(
            task_type="ordinary_review",
            outcome="completed",
            codegraph_calls=0,
            source_mcp_calls=0,
            findings_identified=0,
            findings_confirmed=0,
            findings_rejected=0,
            repeated_source_reads=0,
            deep_analysis_used=True,
            deep_model="secret/path-or-issue-key",
            deep_reasoning="high",
        )


def test_service_exposes_orchestration_state_machine(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    started = service.start_qa_orchestration("ordinary_review")
    advanced = service.advance_qa_orchestration(
        AdvanceQaOrchestrationRequest(
            run_id=started.run_id,
            completed_step=OrchestrationStep.LUNA_TRIAGE,
            status="completed",
            selected_profile="code_reviewer",
        )
    )

    assert advanced.current_step is OrchestrationStep.TERRA_PRIMARY_REVIEW
    assert service.get_qa_orchestration(started.run_id).run_id == started.run_id


def test_metrics_reject_negative_orchestration_counter(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    with pytest.raises(ValueError, match="QA task metrics are inconsistent"):
        service.record_qa_task_outcome(
            task_type="ordinary_review",
            outcome="completed",
            codegraph_calls=0,
            source_mcp_calls=0,
            findings_identified=0,
            findings_confirmed=0,
            findings_rejected=0,
            repeated_source_reads=0,
            orchestration_used=True,
            luna_calls=-1,
        )


def test_metrics_reject_sol_calls_without_deep_analysis(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    with pytest.raises(ValueError, match="QA task metrics are inconsistent"):
        service.record_qa_task_outcome(
            task_type="ordinary_review",
            outcome="partial",
            codegraph_calls=0,
            source_mcp_calls=0,
            findings_identified=0,
            findings_confirmed=0,
            findings_rejected=0,
            repeated_source_reads=0,
            orchestration_used=True,
            luna_calls=1,
            terra_calls=1,
            sol_calls=1,
            orchestration_steps_completed=2,
        )


def test_metrics_reject_completed_orchestration_without_required_stages(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    with pytest.raises(ValueError, match="QA task metrics are inconsistent"):
        service.record_qa_task_outcome(
            task_type="ordinary_review",
            outcome="completed",
            codegraph_calls=0,
            source_mcp_calls=0,
            findings_identified=0,
            findings_confirmed=0,
            findings_rejected=0,
            repeated_source_reads=0,
            orchestration_used=True,
            luna_calls=1,
            terra_calls=1,
            sol_calls=0,
            orchestration_steps_completed=2,
        )


def test_service_infers_orchestration_from_run_id_and_records_counters(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    started = service.start_qa_orchestration("ordinary_review")
    primary = service.advance_qa_orchestration(
        AdvanceQaOrchestrationRequest(
            run_id=started.run_id,
            completed_step=OrchestrationStep.LUNA_TRIAGE,
            status="completed",
            selected_profile="code_reviewer",
        )
    )
    synthesis = service.advance_qa_orchestration(
        AdvanceQaOrchestrationRequest(
            run_id=started.run_id,
            completed_step=primary.current_step,
            status="completed",
            completed_profile=ReviewAgent.CODE_REVIEWER,
        )
    )
    service.advance_qa_orchestration(
        AdvanceQaOrchestrationRequest(
            run_id=started.run_id,
            completed_step=synthesis.current_step,
            status="completed",
        )
    )

    receipt = service.record_qa_task_outcome(
        task_type="ordinary_review",
        outcome="completed",
        codegraph_calls=0,
        source_mcp_calls=0,
        findings_identified=0,
        findings_confirmed=0,
        findings_rejected=0,
        repeated_source_reads=0,
        deep_analysis_used=False,
        luna_calls=1,
        terra_calls=2,
        sol_calls=0,
        orchestration_steps_completed=3,
        orchestration_retries=0,
        run_id=started.run_id,
    )

    assert receipt.status == "recorded"
    assert service.get_qa_orchestration(started.run_id).status is OrchestrationStatus.COMPLETED
    assert service.get_qa_orchestration(started.run_id).outcome_recorded is True
    event = json.loads((tmp_path / "metrics.jsonl").read_text(encoding="utf-8"))
    assert event["orchestration_used"] is True
    assert event["terra_calls"] == 2


@pytest.mark.parametrize(
    ("outcome", "expected_action"),
    [
        ("partial", "Host recorded the partial QA outcome."),
        ("blocked", "Host recorded the blocked QA outcome."),
    ],
)
def test_service_marks_terminal_action_after_recording_outcome(
    tmp_path,
    outcome,
    expected_action,
):
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    started = service.start_qa_orchestration("ordinary_review")
    stopped = service.advance_qa_orchestration(
        AdvanceQaOrchestrationRequest(
            run_id=started.run_id,
            completed_step=OrchestrationStep.LUNA_TRIAGE,
            status=outcome,
        )
    )

    receipt = service.record_qa_task_outcome(
        task_type="ordinary_review",
        outcome=outcome,
        codegraph_calls=0,
        source_mcp_calls=0,
        findings_identified=0,
        findings_confirmed=0,
        findings_rejected=0,
        repeated_source_reads=0,
        orchestration_used=True,
        luna_calls=1,
        terra_calls=0,
        sol_calls=0,
        orchestration_steps_completed=1,
        run_id=stopped.run_id,
    )

    final = service.get_qa_orchestration(started.run_id)

    assert receipt.status == "recorded"
    assert final.outcome_recorded is True
    assert final.next_action == expected_action


def test_service_rejects_outcome_with_mismatched_task_type(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    started = service.start_qa_orchestration("ordinary_review")
    primary = service.advance_qa_orchestration(
        AdvanceQaOrchestrationRequest(
            run_id=started.run_id,
            completed_step=OrchestrationStep.LUNA_TRIAGE,
            status="completed",
            selected_profile="code_reviewer",
        )
    )
    synthesis = service.advance_qa_orchestration(
        AdvanceQaOrchestrationRequest(
            run_id=started.run_id,
            completed_step=primary.current_step,
            status="completed",
            completed_profile=ReviewAgent.CODE_REVIEWER,
        )
    )
    service.advance_qa_orchestration(
        AdvanceQaOrchestrationRequest(
            run_id=started.run_id,
            completed_step=synthesis.current_step,
            status="completed",
        )
    )

    with pytest.raises(ValueError, match="task_type"):
        service.record_qa_task_outcome(
            task_type="widget_review",
            outcome="completed",
            codegraph_calls=0,
            source_mcp_calls=0,
            findings_identified=0,
            findings_confirmed=0,
            findings_rejected=0,
            repeated_source_reads=0,
            orchestration_used=True,
            luna_calls=1,
            terra_calls=2,
            sol_calls=0,
            orchestration_steps_completed=3,
            orchestration_retries=0,
            run_id=started.run_id,
        )

    assert service.get_qa_orchestration(started.run_id).status is OrchestrationStatus.AWAITING_HOST_OUTCOME
    assert not (tmp_path / "metrics.jsonl").exists()


def test_service_records_deep_orchestration_counters(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    started = service.start_qa_orchestration("ordinary_review")
    primary = service.advance_qa_orchestration(
        AdvanceQaOrchestrationRequest(
            run_id=started.run_id,
            completed_step=OrchestrationStep.LUNA_TRIAGE,
            status="completed",
            selected_profile="security_reviewer",
        )
    )
    deep = service.advance_qa_orchestration(
        AdvanceQaOrchestrationRequest(
            run_id=started.run_id,
            completed_step=primary.current_step,
            status="completed",
            completed_profile=ReviewAgent.SECURITY_REVIEWER,
            needs_deep_analysis=True,
            reason_code="security_sensitive",
        )
    )
    synthesis = service.advance_qa_orchestration(
        AdvanceQaOrchestrationRequest(
            run_id=started.run_id,
            completed_step=deep.current_step,
            status="completed",
        )
    )
    service.advance_qa_orchestration(
        AdvanceQaOrchestrationRequest(
            run_id=started.run_id,
            completed_step=synthesis.current_step,
            status="completed",
        )
    )

    with pytest.raises(ValueError, match="QA task metrics are inconsistent"):
        service.record_qa_task_outcome(
            task_type="ordinary_review",
            outcome="completed",
            codegraph_calls=0,
            source_mcp_calls=0,
            findings_identified=0,
            findings_confirmed=0,
            findings_rejected=0,
            repeated_source_reads=0,
            deep_analysis_used=True,
            orchestration_used=True,
            luna_calls=1,
            terra_calls=2,
            sol_calls=1,
            orchestration_steps_completed=4,
            run_id=started.run_id,
        )

    receipt = service.record_qa_task_outcome(
        task_type="ordinary_review",
        outcome="completed",
        codegraph_calls=0,
        source_mcp_calls=0,
        findings_identified=0,
        findings_confirmed=0,
        findings_rejected=0,
        repeated_source_reads=0,
        deep_analysis_used=True,
        deep_model="gpt-5.6-sol",
        deep_reasoning="high",
        orchestration_used=True,
        luna_calls=1,
        terra_calls=2,
        sol_calls=1,
        orchestration_steps_completed=4,
        run_id=started.run_id,
    )

    assert receipt.status == "recorded"
    assert service.get_qa_orchestration(started.run_id).status is OrchestrationStatus.COMPLETED


def test_service_records_deep_escalation_decision_and_reasons(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    started = service.start_qa_orchestration("ordinary_review")
    primary = service.advance_qa_orchestration(
        AdvanceQaOrchestrationRequest(
            run_id=started.run_id,
            completed_step=OrchestrationStep.LUNA_TRIAGE,
            status="completed",
            selected_profile="code_reviewer",
        )
    )
    deep = service.advance_qa_orchestration(
        AdvanceQaOrchestrationRequest(
            run_id=started.run_id,
            completed_step=primary.current_step,
            status="completed",
            completed_profile="code_reviewer",
            risk_signals=DeepReviewSignals(
                high_risk_domain=True,
                evidence_uncertain=True,
            ),
        )
    )
    synthesis = service.advance_qa_orchestration(
        AdvanceQaOrchestrationRequest(
            run_id=started.run_id,
            completed_step=deep.current_step,
            status="completed",
        )
    )
    service.advance_qa_orchestration(
        AdvanceQaOrchestrationRequest(
            run_id=started.run_id,
            completed_step=synthesis.current_step,
            status="completed",
        )
    )

    service.record_qa_task_outcome(
        task_type="ordinary_review",
        outcome="completed",
        codegraph_calls=0,
        source_mcp_calls=0,
        findings_identified=0,
        findings_confirmed=0,
        findings_rejected=0,
        repeated_source_reads=0,
        deep_analysis_used=True,
        deep_model="gpt-5.6-sol",
        deep_reasoning="high",
        orchestration_used=True,
        luna_calls=1,
        terra_calls=2,
        sol_calls=1,
        orchestration_steps_completed=4,
        run_id=started.run_id,
    )

    event = json.loads((tmp_path / "metrics.jsonl").read_text(encoding="utf-8"))

    assert event["deep_escalation_recommended"] is True
    assert event["deep_escalation_reason_codes"] == [
        "high_risk_domain",
        "evidence_gap",
    ]


def test_service_records_stage_tokens_scope_and_deep_value_metrics(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    receipt = service.record_qa_task_outcome(
        task_type="ordinary_review",
        outcome="completed",
        codegraph_calls=1,
        source_mcp_calls=2,
        findings_identified=3,
        findings_confirmed=2,
        findings_rejected=1,
        repeated_source_reads=0,
        deep_analysis_used=True,
        deep_model="gpt-5.6-sol",
        deep_reasoning="high",
        deep_input_tokens=30,
        deep_output_tokens=20,
        deep_findings_identified=2,
        deep_findings_new_confirmed=1,
        deep_findings_rejected=1,
        luna_input_tokens=100,
        luna_output_tokens=25,
        terra_primary_input_tokens=240,
        terra_primary_output_tokens=80,
        terra_synthesis_input_tokens=120,
        terra_synthesis_output_tokens=40,
        evidence_packet_tokens=180,
        merge_requests_count=2,
        repositories_count=2,
    )

    assert receipt.status == "recorded"
    event = json.loads((tmp_path / "metrics.jsonl").read_text(encoding="utf-8"))

    assert event["luna_input_tokens"] == 100
    assert event["terra_primary_output_tokens"] == 80
    assert event["terra_synthesis_input_tokens"] == 120
    assert event["evidence_packet_tokens"] == 180
    assert event["merge_requests_count"] == 2
    assert event["repositories_count"] == 2
    assert event["deep_findings_new_confirmed"] == 1


def test_service_rejects_deep_value_without_deep_analysis(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    with pytest.raises(ValueError, match="QA task metrics are inconsistent"):
        service.record_qa_task_outcome(
            task_type="ordinary_review",
            outcome="completed",
            codegraph_calls=0,
            source_mcp_calls=0,
            findings_identified=1,
            findings_confirmed=1,
            findings_rejected=0,
            repeated_source_reads=0,
            deep_findings_identified=1,
            deep_findings_new_confirmed=1,
        )


def test_service_rejects_metrics_from_wrong_orchestration_branch(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    started = service.start_qa_orchestration("ordinary_review")
    service.advance_qa_orchestration(
        AdvanceQaOrchestrationRequest(
            run_id=started.run_id,
            completed_step=OrchestrationStep.LUNA_TRIAGE,
            status="partial",
        )
    )

    with pytest.raises(ValueError, match="QA task metrics are inconsistent"):
        service.record_qa_task_outcome(
            task_type="ordinary_review",
            outcome="partial",
            codegraph_calls=0,
            source_mcp_calls=0,
            findings_identified=0,
            findings_confirmed=0,
            findings_rejected=0,
            repeated_source_reads=0,
            deep_analysis_used=True,
            deep_model="gpt-5.6-sol",
            deep_reasoning="high",
            orchestration_used=True,
            luna_calls=1,
            terra_calls=0,
            sol_calls=1,
            orchestration_steps_completed=1,
            run_id=started.run_id,
        )


def test_service_does_not_duplicate_finalized_orchestration_metric(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    started = service.start_qa_orchestration("ordinary_review")
    primary = service.advance_qa_orchestration(
        AdvanceQaOrchestrationRequest(
            run_id=started.run_id,
            completed_step=OrchestrationStep.LUNA_TRIAGE,
            status="completed",
            selected_profile="code_reviewer",
        )
    )
    synthesis = service.advance_qa_orchestration(
        AdvanceQaOrchestrationRequest(
            run_id=started.run_id,
            completed_step=primary.current_step,
            status="completed",
            completed_profile=ReviewAgent.CODE_REVIEWER,
        )
    )
    service.advance_qa_orchestration(
        AdvanceQaOrchestrationRequest(
            run_id=started.run_id,
            completed_step=synthesis.current_step,
            status="completed",
        )
    )
    metrics = {
        "task_type": "ordinary_review",
        "outcome": "completed",
        "codegraph_calls": 0,
        "source_mcp_calls": 0,
        "findings_identified": 0,
        "findings_confirmed": 0,
        "findings_rejected": 0,
        "repeated_source_reads": 0,
        "orchestration_used": True,
        "luna_calls": 1,
        "terra_calls": 2,
        "sol_calls": 0,
        "orchestration_steps_completed": 3,
        "orchestration_retries": 0,
        "run_id": started.run_id,
    }

    first = service.record_qa_task_outcome(**metrics)
    second = service.record_qa_task_outcome(**metrics)

    assert first.status == "recorded"
    assert second.status == "recorded"
    assert len((tmp_path / "metrics.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_service_rejects_conflicting_finalized_orchestration_payload(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    started = service.start_qa_orchestration("ordinary_review")
    primary = service.advance_qa_orchestration(
        AdvanceQaOrchestrationRequest(
            run_id=started.run_id,
            completed_step=OrchestrationStep.LUNA_TRIAGE,
            status="completed",
            selected_profile="code_reviewer",
        )
    )
    synthesis = service.advance_qa_orchestration(
        AdvanceQaOrchestrationRequest(
            run_id=started.run_id,
            completed_step=primary.current_step,
            status="completed",
            completed_profile=ReviewAgent.CODE_REVIEWER,
        )
    )
    service.advance_qa_orchestration(
        AdvanceQaOrchestrationRequest(
            run_id=started.run_id,
            completed_step=synthesis.current_step,
            status="completed",
        )
    )
    metrics = {
        "task_type": "ordinary_review",
        "outcome": "completed",
        "codegraph_calls": 0,
        "source_mcp_calls": 0,
        "findings_identified": 0,
        "findings_confirmed": 0,
        "findings_rejected": 0,
        "repeated_source_reads": 0,
        "orchestration_used": True,
        "luna_calls": 1,
        "terra_calls": 2,
        "sol_calls": 0,
        "orchestration_steps_completed": 3,
        "orchestration_retries": 0,
        "run_id": started.run_id,
    }

    assert service.record_qa_task_outcome(**metrics).status == "recorded"

    with pytest.raises(ValueError, match="conflicting outcome payload"):
        service.record_qa_task_outcome(**{**metrics, "findings_identified": 1})

    events = (tmp_path / "metrics.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(events) == 1
    assert json.loads(events[0])["findings_identified"] == 0


def test_service_requires_run_id_for_orchestrated_outcome(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    with pytest.raises(ValueError, match="run_id"):
        service.record_qa_task_outcome(
            task_type="ordinary_review",
            outcome="partial",
            codegraph_calls=0,
            source_mcp_calls=0,
            findings_identified=0,
            findings_confirmed=0,
            findings_rejected=0,
            repeated_source_reads=0,
            orchestration_used=True,
        )
