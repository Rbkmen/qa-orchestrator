import json
import os
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

from qa_orchestrator.doctor import collect_checks, main
from qa_orchestrator.session_store import SqliteSessionStore


def test_doctor_checks_are_read_only(monkeypatch, tmp_path):
    data_dir = tmp_path / "nested" / "data"
    monkeypatch.setenv("QA_ORCHESTRATOR_DATA_DIR", str(data_dir))
    monkeypatch.delenv("QA_ORCHESTRATOR_SESSION_STORE_PATH", raising=False)
    monkeypatch.chdir(tmp_path)

    checks = {check.name: check for check in collect_checks()}

    assert checks["python"].ok
    assert checks["dependencies"].ok
    if os.name == "nt":
        expected_launcher_status = (
            "pass"
            if Path(sys.executable).with_name("qa-orchestrator-mcp.exe").exists()
            else "fail"
        )
    else:
        expected_launcher_status = "info"
    assert checks["MCP launcher"].status == expected_launcher_status
    assert checks["session store"].status == "info"
    assert set(checks) == {
        "python",
        "dependencies",
        "model policy",
        "session store",
        "MCP launcher",
    }
    assert not data_dir.exists()


def test_doctor_json_output(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("QA_ORCHESTRATOR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("QA_ORCHESTRATOR_SESSION_STORE_PATH", raising=False)
    assert main(["--json"]) == 0

    output = json.loads(capsys.readouterr().out)

    assert isinstance(output, list)
    assert {item["name"] for item in output} >= {"python", "dependencies"}


def test_doctor_reports_missing_session_store_without_creating_it(monkeypatch, tmp_path):
    path = tmp_path / "not-created.sqlite3"
    monkeypatch.setenv("QA_ORCHESTRATOR_SESSION_STORE_PATH", str(path))
    monkeypatch.chdir(tmp_path)

    checks = {check.name: check for check in collect_checks()}

    assert checks["session store"].status == "info"
    assert not path.exists()


def test_doctor_reads_initialized_session_store_without_changing_it(monkeypatch, tmp_path):
    path = tmp_path / "sessions.sqlite3"
    SqliteSessionStore(path)
    before = path.read_bytes()
    monkeypatch.setenv("QA_ORCHESTRATOR_SESSION_STORE_PATH", str(path))

    checks = {check.name: check for check in collect_checks()}

    assert checks["session store"].status == "pass"
    assert "count" not in checks["session store"].detail
    assert path.read_bytes() == before


def test_doctor_reports_invalid_session_data_without_exposing_it(monkeypatch, tmp_path):
    path = tmp_path / "sessions.sqlite3"
    SqliteSessionStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO sessions (run_id, payload) VALUES (?, ?)",
            ("private-run-id", '{"secret":"private payload"}'),
        )
    monkeypatch.setenv("QA_ORCHESTRATOR_SESSION_STORE_PATH", str(path))

    checks = {check.name: check for check in collect_checks()}

    assert checks["session store"].status == "fail"
    assert "private-run-id" not in checks["session store"].detail
    assert "private payload" not in checks["session store"].detail


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission modes are not available")
def test_doctor_reports_non_private_session_store_permissions(monkeypatch, tmp_path):
    path = tmp_path / "sessions.sqlite3"
    SqliteSessionStore(path)
    path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)
    monkeypatch.setenv("QA_ORCHESTRATOR_SESSION_STORE_PATH", str(path))

    checks = {check.name: check for check in collect_checks()}

    assert checks["session store"].status == "fail"
