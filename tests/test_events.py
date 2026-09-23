import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from qa_orchestrator.config import Settings
from qa_orchestrator.events import read_metrics_lines, valid_qa_task_metrics
from qa_orchestrator.service import OrchestratorService


def _valid_v2_orchestration_event(**updates):
    event = {
        "schema_version": 2,
        "task_type": "ordinary_review",
        "outcome": "completed",
        "deep_analysis_used": False,
        "orchestration_used": True,
        "codegraph_calls": 0,
        "source_mcp_calls": 0,
        "findings_identified": 0,
        "findings_confirmed": 0,
        "findings_rejected": 0,
        "repeated_source_reads": 0,
        "triage_calls": 1,
        "primary_review_calls": 3,
        "deep_review_calls": 0,
        "synthesis_calls": 1,
        "orchestration_steps_completed": 5,
        "orchestration_retries": 0,
    }
    event.update(updates)
    return event


def test_v2_completed_orchestration_accepts_stage_counters():
    assert valid_qa_task_metrics(_valid_v2_orchestration_event())


@pytest.mark.parametrize("schema_version", [True, 3, 0, "2"])
def test_metrics_reject_unsupported_schema_versions(schema_version):
    assert not valid_qa_task_metrics(
        _valid_v2_orchestration_event(schema_version=schema_version)
    )


@pytest.mark.parametrize("field,value", [("task_type", []), ("outcome", {})])
def test_metrics_reject_unhashable_task_and_outcome_values(field, value):
    assert not valid_qa_task_metrics(_valid_v2_orchestration_event(**{field: value}))


@pytest.mark.parametrize(
    "field",
    ["triage_calls", "primary_review_calls", "deep_review_calls", "synthesis_calls"],
)
@pytest.mark.parametrize("value", [-1, True])
def test_v2_rejects_invalid_stage_call_counters(field, value):
    assert not valid_qa_task_metrics(_valid_v2_orchestration_event(**{field: value}))


@pytest.mark.parametrize(
    "field",
    [
        "triage_input_tokens",
        "triage_output_tokens",
        "primary_review_input_tokens",
        "primary_review_output_tokens",
        "synthesis_input_tokens",
        "synthesis_output_tokens",
    ],
)
@pytest.mark.parametrize("value", [-1, True])
def test_v2_rejects_invalid_stage_token_counters(field, value):
    assert not valid_qa_task_metrics(_valid_v2_orchestration_event(**{field: value}))


@pytest.mark.parametrize(
    "field",
    [
        "luna_calls",
        "terra_calls",
        "sol_calls",
        "luna_input_tokens",
        "luna_output_tokens",
        "terra_primary_input_tokens",
        "terra_primary_output_tokens",
        "terra_synthesis_input_tokens",
        "terra_synthesis_output_tokens",
    ],
)
def test_v2_rejects_legacy_model_counters(field):
    assert not valid_qa_task_metrics(_valid_v2_orchestration_event(**{field: 1}))


@pytest.mark.parametrize(
    "field",
    [
        "triage_calls",
        "primary_review_calls",
        "deep_review_calls",
        "synthesis_calls",
        "triage_input_tokens",
        "triage_output_tokens",
        "primary_review_input_tokens",
        "primary_review_output_tokens",
        "synthesis_input_tokens",
        "synthesis_output_tokens",
    ],
)
def test_v1_rejects_v2_stage_counters(field):
    event = _valid_v2_orchestration_event(schema_version=1)
    for stage_field in (
        "triage_calls",
        "primary_review_calls",
        "deep_review_calls",
        "synthesis_calls",
    ):
        event.pop(stage_field)
    event.update(luna_calls=1, terra_calls=4, sol_calls=0)
    event[field] = 0

    assert not valid_qa_task_metrics(event)


def test_v1_legacy_event_without_schema_version_remains_valid():
    event = _valid_v2_orchestration_event()
    event.pop("schema_version")
    for field in (
        "triage_calls",
        "primary_review_calls",
        "deep_review_calls",
        "synthesis_calls",
    ):
        event.pop(field)
    event.update(
        luna_calls=1,
        terra_calls=4,
        sol_calls=0,
    )

    assert valid_qa_task_metrics(event)


def test_v1_history_keeps_legacy_deep_model_attribution():
    event = _valid_v2_orchestration_event(
        deep_analysis_used=True,
        deep_model="gpt-5.6-sol",
        deep_reasoning="high",
        deep_review_calls=1,
    )
    event.pop("schema_version")
    for field in (
        "triage_calls",
        "primary_review_calls",
        "deep_review_calls",
        "synthesis_calls",
    ):
        event.pop(field)
    event.update(luna_calls=1, terra_calls=4, sol_calls=1)

    assert valid_qa_task_metrics(event)


def test_v2_deep_review_requires_selected_branch_model_and_reasoning():
    event = _valid_v2_orchestration_event(
        deep_analysis_used=True,
        deep_review_calls=1,
        deep_model="gpt-6-sol",
        deep_reasoning="high",
        synthesis_calls=1,
        orchestration_steps_completed=6,
    )

    assert valid_qa_task_metrics(event)
    # This is a call count within the selected branch, not a boolean encoded as 1.
    assert valid_qa_task_metrics({**event, "deep_review_calls": 2})
    assert not valid_qa_task_metrics({**event, "deep_review_calls": 0})
    assert valid_qa_task_metrics({**event, "deep_model": "gpt-5.6-sol"})
    assert not valid_qa_task_metrics({**event, "deep_model": "secret/path-or-issue-key"})
    assert valid_qa_task_metrics({**event, "deep_reasoning": "medium"})


@pytest.mark.parametrize("outcome", ["partial", "blocked"])
def test_v2_incomplete_orchestration_may_stop_before_synthesis(outcome):
    event = _valid_v2_orchestration_event(
        outcome=outcome,
        triage_calls=1,
        primary_review_calls=0,
        synthesis_calls=0,
        orchestration_steps_completed=1,
    )

    assert valid_qa_task_metrics(event)


def test_v2_non_orchestrated_event_rejects_stage_calls():
    event = _valid_v2_orchestration_event(
        orchestration_used=False,
        triage_calls=0,
        primary_review_calls=1,
        deep_review_calls=0,
        synthesis_calls=0,
        orchestration_steps_completed=0,
    )

    assert not valid_qa_task_metrics(event)


def _record_minimal_outcome(service: OrchestratorService) -> None:
    receipt = service.record_qa_task_outcome(
        task_type="ordinary_review",
        outcome="completed",
        codegraph_calls=0,
        source_mcp_calls=0,
        findings_identified=0,
        findings_confirmed=0,
        findings_rejected=0,
        repeated_source_reads=0,
    )
    assert receipt.status == "recorded"


def test_metrics_retention_and_max_events_are_enforced(tmp_path):
    old_timestamp = (datetime.now(UTC) - timedelta(days=31)).isoformat()
    metrics_path = tmp_path / "metrics.jsonl"
    metrics_path.write_text(json.dumps({"timestamp": old_timestamp}) + "\n", encoding="utf-8")
    service = OrchestratorService(
        Settings(
            data_dir=tmp_path,
            metrics_retention_days=30,
            metrics_max_events=2,
        )
    )

    _record_minimal_outcome(service)
    _record_minimal_outcome(service)

    events = [json.loads(line) for line in metrics_path.read_text(encoding="utf-8").splitlines()]
    assert len(events) == 2
    assert all(event["timestamp"] != old_timestamp for event in events)


def test_metrics_rewrite_discards_malformed_lines(tmp_path):
    metrics_path = tmp_path / "metrics.jsonl"
    metrics_path.write_text("not-json\n", encoding="utf-8")
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    _record_minimal_outcome(service)

    lines = metrics_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event_type"] == "qa_task_outcome"


def test_malformed_utf8_metrics_are_discarded_on_next_write(tmp_path):
    metrics_path = tmp_path / "metrics.jsonl"
    metrics_path.write_bytes(b"\xff\n")
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    _record_minimal_outcome(service)

    lines = metrics_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event_type"] == "qa_task_outcome"


def test_reading_metrics_does_not_create_a_lock_file(tmp_path):
    metrics_path = tmp_path / "metrics.jsonl"
    metrics_path.write_text("{}\n", encoding="utf-8")

    assert read_metrics_lines(metrics_path) == ["{}"]
    assert not metrics_path.with_name(".metrics.jsonl.lock").exists()


@pytest.mark.skipif(os.name == "nt", reason="metrics permissions use POSIX modes")
def test_metrics_storage_uses_restricted_permissions(tmp_path):
    data_dir = tmp_path / "metrics"
    service = OrchestratorService.from_settings(data_dir=data_dir)

    _record_minimal_outcome(service)

    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((data_dir / "metrics.jsonl").stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="metrics permissions use POSIX modes")
def test_metrics_do_not_change_existing_shared_data_dir_permissions(tmp_path):
    data_dir = tmp_path / "shared"
    data_dir.mkdir()
    data_dir.chmod(0o755)
    service = OrchestratorService.from_settings(data_dir=data_dir)

    receipt = service.record_qa_task_outcome(
        task_type="ordinary_review",
        outcome="completed",
        codegraph_calls=0,
        source_mcp_calls=0,
        findings_identified=0,
        findings_confirmed=0,
        findings_rejected=0,
        repeated_source_reads=0,
    )

    assert receipt.status == "unavailable"
    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o755
    assert not (data_dir / "metrics.jsonl").exists()


def test_concurrent_metrics_writers_preserve_each_event(tmp_path):
    services = [OrchestratorService.from_settings(data_dir=tmp_path) for _ in range(8)]

    with ThreadPoolExecutor(max_workers=len(services)) as executor:
        list(executor.map(_record_minimal_outcome, services))

    lines = (tmp_path / "metrics.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(services)
    assert all(json.loads(line)["event_type"] == "qa_task_outcome" for line in lines)
