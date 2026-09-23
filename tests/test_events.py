import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from qa_orchestrator.config import Settings
from qa_orchestrator.events import (
    DISTRIBUTION_EVENT_TYPE,
    DISTRIBUTION_SCHEMA_VERSION,
    JsonEventSink,
    normalize_task_distribution_event,
    read_metrics_lines,
    valid_task_distribution_event,
)
from qa_orchestrator.service import OrchestratorService


def _distribution_event(**updates):
    event = {
        "schema_version": DISTRIBUTION_SCHEMA_VERSION,
        "event_type": DISTRIBUTION_EVENT_TYPE,
        "timestamp": datetime.now(UTC).isoformat(),
        "task_type": "ordinary_review",
    }
    event.update(updates)
    return event


def test_distribution_event_accepts_only_task_type_and_timestamp():
    assert valid_task_distribution_event(_distribution_event())
    assert not valid_task_distribution_event(_distribution_event(outcome="completed"))
    assert not valid_task_distribution_event(_distribution_event(extra_metric=1))


@pytest.mark.parametrize("schema_version", [True, 1, 2, 4, "3"])
def test_distribution_rejects_unsupported_schema_versions(schema_version):
    assert not valid_task_distribution_event(_distribution_event(schema_version=schema_version))


@pytest.mark.parametrize("task_type", [[], {}, "not_a_task_type"])
def test_distribution_rejects_invalid_task_types(task_type):
    assert not valid_task_distribution_event(_distribution_event(task_type=task_type))


@pytest.mark.parametrize("schema_version", [1, 2, None])
def test_legacy_outcome_is_normalized_to_distribution_only(schema_version):
    timestamp = datetime.now(UTC).isoformat()
    legacy_event = {
        "event_type": "qa_task_outcome",
        "timestamp": timestamp,
        "task_type": "widget_review",
        "outcome": "completed",
        "private_metric": 123,
    }
    if schema_version is not None:
        legacy_event["schema_version"] = schema_version

    assert normalize_task_distribution_event(legacy_event) == {
        "schema_version": DISTRIBUTION_SCHEMA_VERSION,
        "event_type": DISTRIBUTION_EVENT_TYPE,
        "timestamp": timestamp,
        "task_type": "widget_review",
    }


def _record_task(service: OrchestratorService, task_type: str = "ordinary_review") -> None:
    receipt = service.record_qa_task_outcome(task_type=task_type, outcome="completed")
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

    _record_task(service)
    _record_task(service, "widget_review")
    _record_task(service)

    events = [json.loads(line) for line in metrics_path.read_text(encoding="utf-8").splitlines()]
    assert len(events) == 2
    assert all(event["timestamp"] != old_timestamp for event in events)


def test_next_write_migrates_old_records_and_drops_unneeded_fields(tmp_path):
    old_timestamp = datetime.now(UTC).isoformat()
    metrics_path = tmp_path / "metrics.jsonl"
    metrics_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "event_type": "qa_task_outcome",
                "timestamp": old_timestamp,
                "task_type": "widget_review",
                "outcome": "completed",
                "private_metric": 123,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    _record_task(service)

    events = [json.loads(line) for line in metrics_path.read_text(encoding="utf-8").splitlines()]
    assert events[0] == {
        "schema_version": DISTRIBUTION_SCHEMA_VERSION,
        "event_type": DISTRIBUTION_EVENT_TYPE,
        "timestamp": old_timestamp,
        "task_type": "widget_review",
    }
    assert valid_task_distribution_event(events[1])
    assert events[1]["task_type"] == "ordinary_review"


def test_startup_sanitizer_rewrites_existing_data_to_distribution_only(tmp_path):
    old_timestamp = datetime.now(UTC).isoformat()
    older_timestamp = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    metrics_path = tmp_path / "metrics.jsonl"
    metrics_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "event_type": "qa_task_outcome",
                "timestamp": older_timestamp,
                "task_type": "ordinary_review",
                "outcome": "completed",
                "legacy_metric": 1234,
            }
        )
        + "\n"
        + json.dumps(
            {
                "schema_version": 2,
                "event_type": "qa_task_outcome",
                "timestamp": old_timestamp,
                "task_type": "widget_review",
                "outcome": "completed",
                "extra_metric": 1234,
                "model": "private-model-detail",
            }
        )
        + "\nnot-json\n",
        encoding="utf-8",
    )

    assert JsonEventSink(metrics_path).sanitize_existing_records()

    events = [json.loads(line) for line in metrics_path.read_text(encoding="utf-8").splitlines()]
    assert events == [
        {
            "schema_version": DISTRIBUTION_SCHEMA_VERSION,
            "event_type": DISTRIBUTION_EVENT_TYPE,
            "timestamp": older_timestamp,
            "task_type": "ordinary_review",
        },
        {
            "schema_version": DISTRIBUTION_SCHEMA_VERSION,
            "event_type": DISTRIBUTION_EVENT_TYPE,
            "timestamp": old_timestamp,
            "task_type": "widget_review",
        }
    ]


@pytest.mark.skipif(os.name == "nt", reason="metrics directory permissions use POSIX modes")
def test_startup_sanitizer_checks_directory_permissions_without_existing_metrics(tmp_path):
    data_dir = tmp_path / "shared"
    data_dir.mkdir()
    data_dir.chmod(0o755)

    assert not JsonEventSink(data_dir / "metrics.jsonl").sanitize_existing_records()


@pytest.mark.skipif(os.name == "nt", reason="metrics symlinks use POSIX semantics")
def test_startup_sanitizer_rejects_symlinked_directory_without_existing_metrics(tmp_path):
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    target_dir.chmod(0o700)
    data_dir = tmp_path / "shared"
    data_dir.symlink_to(target_dir, target_is_directory=True)

    assert not JsonEventSink(data_dir / "metrics.jsonl").sanitize_existing_records()


def test_task_distribution_write_does_not_log_event_to_stderr(capsys, tmp_path):
    receipt = JsonEventSink(tmp_path / "metrics.jsonl").record_qa_task_outcome(
        {"task_type": "ordinary_review"}
    )

    assert receipt.status == "recorded"
    assert capsys.readouterr().err == ""


def test_metrics_rewrite_discards_malformed_lines(tmp_path):
    metrics_path = tmp_path / "metrics.jsonl"
    metrics_path.write_text("not-json\n", encoding="utf-8")
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    _record_task(service)

    lines = metrics_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event_type"] == DISTRIBUTION_EVENT_TYPE


def test_malformed_utf8_metrics_are_discarded_on_next_write(tmp_path):
    metrics_path = tmp_path / "metrics.jsonl"
    metrics_path.write_bytes(b"\xff\n")
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    _record_task(service)

    lines = metrics_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event_type"] == DISTRIBUTION_EVENT_TYPE


def test_reading_metrics_does_not_create_a_lock_file(tmp_path):
    metrics_path = tmp_path / "metrics.jsonl"
    metrics_path.write_text("{}\n", encoding="utf-8")

    assert read_metrics_lines(metrics_path) == ["{}"]
    assert not metrics_path.with_name(".metrics.jsonl.lock").exists()


@pytest.mark.skipif(os.name == "nt", reason="metrics permissions use POSIX modes")
def test_metrics_storage_uses_restricted_permissions(tmp_path):
    data_dir = tmp_path / "metrics"
    service = OrchestratorService.from_settings(data_dir=data_dir)

    _record_task(service)

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
    )

    assert receipt.status == "unavailable"
    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o755
    assert not (data_dir / "metrics.jsonl").exists()


def test_concurrent_metrics_writers_preserve_each_event(tmp_path):
    services = [OrchestratorService.from_settings(data_dir=tmp_path) for _ in range(8)]

    with ThreadPoolExecutor(max_workers=len(services)) as executor:
        list(executor.map(_record_task, services))

    lines = (tmp_path / "metrics.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(services)
    assert all(json.loads(line)["event_type"] == DISTRIBUTION_EVENT_TYPE for line in lines)
