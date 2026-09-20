import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from qa_router_mcp.config import Settings
from qa_router_mcp.events import read_metrics_lines
from qa_router_mcp.service import RouterService


def _record_minimal_outcome(service: RouterService) -> None:
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
    service = RouterService(
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
    service = RouterService.from_settings(data_dir=tmp_path)

    _record_minimal_outcome(service)

    lines = metrics_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event_type"] == "qa_task_outcome"


def test_malformed_utf8_metrics_are_discarded_on_next_write(tmp_path):
    metrics_path = tmp_path / "metrics.jsonl"
    metrics_path.write_bytes(b"\xff\n")
    service = RouterService.from_settings(data_dir=tmp_path)

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
    service = RouterService.from_settings(data_dir=tmp_path)

    _record_minimal_outcome(service)

    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "metrics.jsonl").stat().st_mode) == 0o600


def test_concurrent_metrics_writers_preserve_each_event(tmp_path):
    services = [RouterService.from_settings(data_dir=tmp_path) for _ in range(8)]

    with ThreadPoolExecutor(max_workers=len(services)) as executor:
        list(executor.map(_record_minimal_outcome, services))

    lines = (tmp_path / "metrics.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(services)
    assert all(json.loads(line)["event_type"] == "qa_task_outcome" for line in lines)
