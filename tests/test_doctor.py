import json
import os

import pytest

from qa_orchestrator.doctor import collect_checks, main


def test_doctor_checks_are_read_only(monkeypatch, tmp_path):
    data_dir = tmp_path / "nested" / "metrics"
    monkeypatch.setenv("QA_ORCHESTRATOR_DATA_DIR", str(data_dir))
    monkeypatch.chdir(tmp_path)

    checks = {check.name: check for check in collect_checks()}

    assert checks["python"].ok
    assert checks["dependencies"].ok
    assert checks["metrics directory"].ok
    assert checks["source launcher"].status == "info"
    assert not data_dir.exists()


def test_doctor_json_output(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("QA_ORCHESTRATOR_DATA_DIR", str(tmp_path / "metrics"))
    assert main(["--json"]) == 0

    output = json.loads(capsys.readouterr().out)

    assert isinstance(output, list)
    assert {item["name"] for item in output} >= {"python", "dependencies", "metrics directory"}


@pytest.mark.skipif(os.name == "nt", reason="metrics directory permissions use POSIX modes")
def test_doctor_rejects_shared_metrics_directory(monkeypatch, tmp_path):
    data_dir = tmp_path / "metrics"
    data_dir.mkdir()
    data_dir.chmod(0o755)
    monkeypatch.setenv("QA_ORCHESTRATOR_DATA_DIR", str(data_dir))

    checks = {check.name: check for check in collect_checks()}

    assert not checks["metrics directory"].ok
    assert "private" in checks["metrics directory"].detail


@pytest.mark.skipif(os.name == "nt", reason="metrics symlinks use POSIX semantics")
def test_doctor_rejects_metrics_file_symlink(monkeypatch, tmp_path):
    data_dir = tmp_path / "metrics"
    data_dir.mkdir()
    data_dir.chmod(0o700)
    outside_file = tmp_path / "outside.jsonl"
    outside_file.write_text("{}\n", encoding="utf-8")
    (data_dir / "metrics.jsonl").symlink_to(outside_file)
    monkeypatch.setenv("QA_ORCHESTRATOR_DATA_DIR", str(data_dir))

    checks = {check.name: check for check in collect_checks()}

    assert not checks["metrics directory"].ok
    assert "symlink" in checks["metrics directory"].detail
