import json

from qa_orchestrator.doctor import collect_checks, main


def test_doctor_checks_are_read_only(monkeypatch, tmp_path):
    data_dir = tmp_path / "nested" / "data"
    monkeypatch.setenv("QA_ORCHESTRATOR_DATA_DIR", str(data_dir))
    monkeypatch.chdir(tmp_path)

    checks = {check.name: check for check in collect_checks()}

    assert checks["python"].ok
    assert checks["dependencies"].ok
    assert checks["source launcher"].status == "info"
    assert set(checks) == {"python", "dependencies", "model policy", "source launcher"}
    assert not data_dir.exists()


def test_doctor_json_output(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("QA_ORCHESTRATOR_DATA_DIR", str(tmp_path / "data"))
    assert main(["--json"]) == 0

    output = json.loads(capsys.readouterr().out)

    assert isinstance(output, list)
    assert {item["name"] for item in output} >= {"python", "dependencies"}
