import re
from dataclasses import fields
from pathlib import Path

import pytest

from qa_orchestrator.config import Settings


def test_settings_use_metrics_defaults(monkeypatch):
    monkeypatch.delenv("QA_ORCHESTRATOR_DATA_DIR", raising=False)
    monkeypatch.delenv("QA_ORCHESTRATOR_METRICS_RETENTION_DAYS", raising=False)
    monkeypatch.delenv("QA_ORCHESTRATOR_METRICS_MAX_EVENTS", raising=False)
    monkeypatch.delenv("QA_ORCHESTRATOR_ORCHESTRATION_TTL_SECONDS", raising=False)
    monkeypatch.delenv("QA_ORCHESTRATOR_ORCHESTRATION_MAX_SESSIONS", raising=False)

    settings = Settings.from_env()

    assert settings.metrics_retention_days == 30
    assert settings.metrics_max_events == 10_000
    assert settings.orchestration_session_ttl_seconds == 1_800
    assert settings.orchestration_max_sessions == 100
    assert settings.metrics_path.name == "metrics.jsonl"


def test_settings_reject_non_positive_metrics():
    with pytest.raises(ValueError, match="metrics retention"):
        Settings(metrics_retention_days=0)
    with pytest.raises(ValueError, match="metrics retention"):
        Settings(metrics_max_events=0)


def test_settings_reject_non_positive_orchestration_limits():
    with pytest.raises(ValueError, match="orchestration"):
        Settings(orchestration_session_ttl_seconds=0)
    with pytest.raises(ValueError, match="orchestration"):
        Settings(orchestration_max_sessions=0)


def test_settings_read_orchestration_overrides(monkeypatch):
    monkeypatch.setenv("QA_ORCHESTRATOR_ORCHESTRATION_TTL_SECONDS", "90")
    monkeypatch.setenv("QA_ORCHESTRATOR_ORCHESTRATION_MAX_SESSIONS", "7")

    settings = Settings.from_env()

    assert settings.orchestration_session_ttl_seconds == 90
    assert settings.orchestration_max_sessions == 7


def test_settings_have_no_model_runtime_configuration():
    settings = Settings(data_dir=Path("/tmp/qa-orchestrator-test"))

    assert {field.name for field in fields(settings)} == {
        "metrics_retention_days",
        "metrics_max_events",
        "orchestration_session_ttl_seconds",
        "orchestration_max_sessions",
        "data_dir",
    }


def test_launcher_does_not_reference_model_runtime():
    root = Path(__file__).parents[1]
    content = (root / "scripts/qa-orchestrator").read_text(encoding="utf-8")

    assert set(re.findall(r"QA_ORCHESTRATOR_[A-Z0-9_]+", content)) <= {
        "QA_ORCHESTRATOR_METRICS_RETENTION_DAYS",
        "QA_ORCHESTRATOR_METRICS_MAX_EVENTS",
        "QA_ORCHESTRATOR_ORCHESTRATION_TTL_SECONDS",
        "QA_ORCHESTRATOR_ORCHESTRATION_MAX_SESSIONS",
        "QA_ORCHESTRATOR_DATA_DIR",
        "QA_ORCHESTRATOR_MODEL_POLICY_PATH",
    }
