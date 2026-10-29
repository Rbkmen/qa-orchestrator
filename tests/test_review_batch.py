import json

from qa_orchestrator.evidence import evidence_main


def test_batch_prepares_each_input_and_reports_failures(tmp_path, bound_review_context, capsys):
    diff = b"diff --git a/src/service.py b/src/service.py\n--- a/src/service.py\n+++ b/src/service.py\n@@ -1 +1 @@\n-old\n+new\n"
    import hashlib

    data = bound_review_context.model_dump(mode="json")
    data.update(
        input_kind="diff",
        base_sha=None,
        head_sha=None,
        diff_sha256=hashlib.sha256(diff).hexdigest(),
    )
    data.pop("diff_hunk_inventory_sha256", None)
    data.pop("diff_hunks", None)
    data["evidence_registry"] = [
        {"evidence_ref": "E1", "kind": "diff", "sha256": data["diff_sha256"]},
        {
            "evidence_ref": "E2",
            "kind": "other",
            "sha256": hashlib.sha256(b"context").hexdigest(),
            "locator": "host:context",
        },
    ]
    (tmp_path / "context.json").write_text(json.dumps(data))
    (tmp_path / "change.diff").write_bytes(diff)
    (tmp_path / "content.json").write_text(json.dumps({"E2": "context"}))
    manifest = {
        "inputs": [
            {
                "id": "mr1",
                "kind": "diff",
                "context": "context.json",
                "diff": "change.diff",
                "content": "content.json",
                "packet": "packet.json",
                "bound_context": "bound.json",
            },
            {
                "id": "mr2",
                "kind": "git",
                "repository": "missing",
                "base": "main",
                "head": "feature",
            },
        ]
    }
    (tmp_path / "batch.json").write_text(json.dumps(manifest))
    assert evidence_main(["prepare-batch", "--manifest", str(tmp_path / "batch.json")]) == 1
    results = json.loads(capsys.readouterr().out)["results"]
    assert results[0]["status"] == "ready"
    assert (tmp_path / "bound.json").exists()
    assert results[1]["status"] == "failed"
    assert results[1]["step"] == "resolve-git"
    assert results[1]["reason"]


def test_final_gate_requires_a_terminal_session_or_specific_failure(tmp_path, capsys):
    manifest = {"inputs": [{"id": "mr1"}, {"id": "mr2"}, {"id": "mr3"}]}
    records = {
        "results": [
            {"id": "mr1", "status": "finalized", "run_id": "qar-123", "outcome": "partial"},
            {"id": "mr2", "status": "failed", "step": "preflight", "reason": "diff unavailable"},
        ]
    }
    (tmp_path / "batch.json").write_text(json.dumps(manifest))
    (tmp_path / "results.json").write_text(json.dumps(records))
    assert (
        evidence_main(
            [
                "check-batch",
                "--manifest",
                str(tmp_path / "batch.json"),
                "--results",
                str(tmp_path / "results.json"),
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["unresolved"] == ["mr3"]


def test_final_gate_rejects_incomplete_and_duplicate_records(tmp_path, capsys):
    manifest = {"inputs": [{"id": "a"}, {"id": "b"}]}
    (tmp_path / "manifest").write_text(json.dumps(manifest))
    (tmp_path / "results").write_text(
        json.dumps(
            {
                "results": [
                    {"id": "a", "status": "finalized", "outcome": "completed"},
                    {"id": "b", "status": "failed", "step": "preflight", "reason": "  "},
                ]
            }
        )
    )
    assert (
        evidence_main(
            [
                "check-batch",
                "--manifest",
                str(tmp_path / "manifest"),
                "--results",
                str(tmp_path / "results"),
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["unresolved"] == ["a", "b"]


def test_final_gate_accepts_recorded_terminal_and_observed_failure(tmp_path, capsys):
    (tmp_path / "manifest").write_text(json.dumps({"inputs": [{"id": "a"}, {"id": "b"}]}))
    (tmp_path / "results").write_text(
        json.dumps(
            {
                "results": [
                    {
                        "id": "a",
                        "status": "finalized",
                        "run_id": "qar-test",
                        "outcome": "completed",
                    },
                    {
                        "id": "b",
                        "status": "failed",
                        "step": "pack-diff",
                        "reason": "diff truncated",
                    },
                ]
            }
        )
    )
    assert (
        evidence_main(
            [
                "check-batch",
                "--manifest",
                str(tmp_path / "manifest"),
                "--results",
                str(tmp_path / "results"),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["unresolved"] == []


def test_agent_trace_rejects_final_answer_without_each_mr_accounted_for(tmp_path, capsys):
    (tmp_path / "manifest").write_text(json.dumps({"inputs": [{"id": "a"}, {"id": "b"}]}))
    events = {
        "events": [
            {"type": "tool_error", "tool": "qa-orch", "input_id": "a", "reason": "not in PATH"},
            {"type": "tool_error", "tool": "codegraph", "input_id": "a", "reason": "unavailable"},
            {"type": "start", "input_id": "a", "run_id": "run-a"},
            {"type": "finish", "input_id": "a", "run_id": "run-a", "outcome": "partial"},
            {"type": "final"},
        ]
    }
    (tmp_path / "trace").write_text(json.dumps(events))
    assert (
        evidence_main(
            [
                "check-agent-trace",
                "--manifest",
                str(tmp_path / "manifest"),
                "--trace",
                str(tmp_path / "trace"),
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["unresolved"] == ["b"]
    events["events"].insert(-1, {"type": "start", "input_id": "b", "run_id": "run-b"})
    events["events"].insert(
        -1, {"type": "finish", "input_id": "b", "run_id": "run-b", "outcome": "partial"}
    )
    (tmp_path / "trace").write_text(json.dumps(events))
    assert (
        evidence_main(
            [
                "check-agent-trace",
                "--manifest",
                str(tmp_path / "manifest"),
                "--trace",
                str(tmp_path / "trace"),
            ]
        )
        == 0
    )


def test_batch_rejects_output_overwriting_another_input(tmp_path):
    (tmp_path / "input.diff").write_text("original")
    manifest = {
        "inputs": [
            {
                "id": "a",
                "kind": "diff",
                "context": "ctx",
                "diff": "input.diff",
                "content": "content",
                "packet": "packet",
                "bound_context": "bound",
            },
            {
                "id": "b",
                "kind": "diff",
                "context": "ctx2",
                "diff": "diff2",
                "content": "content2",
                "packet": "input.diff",
                "bound_context": "bound2",
            },
        ]
    }
    (tmp_path / "manifest").write_text(json.dumps(manifest))
    import pytest

    with pytest.raises(ValueError, match="distinct"):
        evidence_main(["prepare-batch", "--manifest", str(tmp_path / "manifest")])
    assert (tmp_path / "input.diff").read_text() == "original"


def test_trace_ignores_unobserved_failure_and_events_after_final(tmp_path, capsys):
    (tmp_path / "manifest").write_text(json.dumps({"inputs": [{"id": "a"}]}))
    (tmp_path / "trace").write_text(
        json.dumps(
            {
                "events": [
                    {
                        "type": "failure",
                        "input_id": "a",
                        "step": "preflight",
                        "reason": "unknown",
                        "observed": False,
                    },
                    {"type": "final"},
                    {
                        "type": "failure",
                        "input_id": "a",
                        "step": "preflight",
                        "reason": "unavailable",
                        "observed": True,
                    },
                ]
            }
        )
    )
    assert (
        evidence_main(
            [
                "check-agent-trace",
                "--manifest",
                str(tmp_path / "manifest"),
                "--trace",
                str(tmp_path / "trace"),
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["unresolved"] == ["a"]


async def test_mcp_sessions_remain_available_when_cli_and_codegraph_are_missing(
    tmp_path, bound_review_context, monkeypatch, capsys
):
    from fastmcp import Client

    from qa_orchestrator.server import build_server
    from qa_orchestrator.service import OrchestratorService

    monkeypatch.setenv("PATH", "")
    manifest = {"inputs": [{"id": "mr1"}, {"id": "mr2"}]}
    (tmp_path / "manifest").write_text(json.dumps(manifest))
    data = bound_review_context.model_dump(mode="json")
    data["input_manifest"].update(
        schema_version=2, source_inventory_status="partial", source_conflict_status="unknown"
    )
    events = [
        {"type": "tool_error", "input_id": "mr1", "tool": "qa-orch", "reason": "not in PATH"},
        {"type": "tool_error", "input_id": "mr1", "tool": "codegraph", "reason": "unavailable"},
    ]
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    async with Client(build_server(service)) as client:
        for identity in ("mr1", "mr2"):
            started = await client.call_tool(
                "start_qa_orchestration", {"task_type": "ordinary_review", "review_context": data}
            )
            run_id = started.structured_content["run_id"]
            events.append({"type": "start", "input_id": identity, "run_id": run_id})
            await client.call_tool(
                "advance_qa_orchestration",
                {
                    "run_id": run_id,
                    "completed_step": "triage",
                    "status": "partial",
                    "stop_reason": "test stop",
                },
            )
            finished = await client.call_tool(
                "finish_qa_orchestration", {"run_id": run_id, "outcome": "partial"}
            )
            events.append(
                {
                    "type": "finish",
                    "input_id": identity,
                    "run_id": run_id,
                    "outcome": finished.structured_content["status"],
                }
            )
    events.append({"type": "final"})
    (tmp_path / "trace").write_text(json.dumps({"events": events}))
    assert (
        evidence_main(
            [
                "check-agent-trace",
                "--manifest",
                str(tmp_path / "manifest"),
                "--trace",
                str(tmp_path / "trace"),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["unresolved"] == []
