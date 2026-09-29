"""Keep tests independent of the user's model policy and recovery store."""

import pytest


@pytest.fixture(autouse=True)
def isolated_orchestrator_environment(monkeypatch, tmp_path):
    for name in (
        "QA_ORCHESTRATOR_MODEL_POLICY_PATH",
        "QA_ORCHESTRATOR_SESSION_STORE_PATH",
        "QA_ORCHESTRATOR_ORCHESTRATION_TTL_SECONDS",
        "QA_ORCHESTRATOR_ORCHESTRATION_MAX_SESSIONS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("QA_ORCHESTRATOR_DATA_DIR", str(tmp_path))
