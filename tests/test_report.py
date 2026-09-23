import json
import sys
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from qa_orchestrator.events import DISTRIBUTION_EVENT_TYPE, DISTRIBUTION_SCHEMA_VERSION
from qa_orchestrator.report import TaskDistributionReport, main, summarize_events


def _distribution_event(task_type="ordinary_review", timestamp=None, **extra):
    return {
        "schema_version": DISTRIBUTION_SCHEMA_VERSION,
        "event_type": DISTRIBUTION_EVENT_TYPE,
        "timestamp": timestamp or datetime.now(UTC).isoformat(),
        "task_type": task_type,
        **extra,
    }


def test_report_skips_invalid_rows_and_returns_only_distribution():
    valid = _distribution_event()
    lines = [
        "not-json",
        json.dumps([]),
        json.dumps({**valid, "task_type": []}),
        json.dumps({**valid, "extra_metric": 1}),
        json.dumps({**valid, "schema_version": 2}),
        json.dumps(valid),
    ]

    report = summarize_events(lines)

    TaskDistributionReport.model_validate(report)
    assert report == {
        "period_days": 7,
        "total_tasks": 1,
        "by_task_type": {"ordinary_review": 1},
    }


@pytest.mark.parametrize("schema_version", [1, 2, None])
def test_report_counts_current_and_sanitized_historical_task_types(schema_version):
    timestamp = datetime.now(UTC).isoformat()
    current = _distribution_event("ordinary_review", timestamp)
    historical = {
        "event_type": "qa_task_outcome",
        "timestamp": timestamp,
        "task_type": "widget_review",
        "outcome": "completed",
        "private_metric": 12,
    }
    if schema_version is not None:
        historical["schema_version"] = schema_version

    report = summarize_events([json.dumps(current), json.dumps(historical)])

    assert report == {
        "period_days": 7,
        "total_tasks": 2,
        "by_task_type": {"ordinary_review": 1, "widget_review": 1},
    }


def test_report_excludes_tasks_outside_requested_window():
    old = _distribution_event(
        "qa_planning",
        (datetime.now(UTC) - timedelta(days=8)).isoformat(),
    )

    report = summarize_events([json.dumps(old)], days=7)

    assert report == {"period_days": 7, "total_tasks": 0, "by_task_type": {}}


def test_report_schema_forbids_unrelated_data():
    report = summarize_events([json.dumps(_distribution_event())])

    with pytest.raises(ValidationError):
        TaskDistributionReport.model_validate({**report, "unrelated_metric": 1})


def test_report_is_not_generated_when_existing_data_cannot_be_sanitized(
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(sys, "argv", ["qa-orchestrator-report"])
    monkeypatch.setattr(
        "qa_orchestrator.report.JsonEventSink.sanitize_existing_records",
        lambda _self: False,
    )

    with pytest.raises(SystemExit) as error:
        main()

    assert error.value.code == 1
    assert "report not generated" in capsys.readouterr().err
