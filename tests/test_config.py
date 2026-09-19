import re
from dataclasses import fields
from pathlib import Path

import pytest

from qa_router_mcp.config import Settings


def test_settings_use_metrics_defaults(monkeypatch):
    monkeypatch.delenv("QA_ROUTER_DATA_DIR", raising=False)
    monkeypatch.delenv("QA_ROUTER_METRICS_RETENTION_DAYS", raising=False)
    monkeypatch.delenv("QA_ROUTER_METRICS_MAX_EVENTS", raising=False)

    settings = Settings.from_env()

    assert settings.metrics_retention_days == 30
    assert settings.metrics_max_events == 10_000
    assert settings.metrics_path.name == "metrics.jsonl"


def test_settings_reject_non_positive_metrics():
    with pytest.raises(ValueError, match="metrics retention"):
        Settings(metrics_retention_days=0)
    with pytest.raises(ValueError, match="metrics retention"):
        Settings(metrics_max_events=0)


def test_settings_have_no_model_runtime_configuration():
    settings = Settings(data_dir=Path("/tmp/qa-router-test"))

    assert {field.name for field in fields(settings)} == {
        "metrics_retention_days",
        "metrics_max_events",
        "data_dir",
    }


def test_launcher_does_not_reference_model_runtime():
    root = Path(__file__).parents[1]
    content = (root / "scripts/qa-router-mcp").read_text(encoding="utf-8")

    assert set(re.findall(r"QA_ROUTER_[A-Z0-9_]+", content)) <= {
        "QA_ROUTER_METRICS_RETENTION_DAYS",
        "QA_ROUTER_METRICS_MAX_EVENTS",
        "QA_ROUTER_DATA_DIR",
    }
