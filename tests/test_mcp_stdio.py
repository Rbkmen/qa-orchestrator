"""Exercise the installed MCP protocol across a real server restart."""

import sys
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from qa_orchestrator.contracts import EvidenceRecord, ReviewAgent


@pytest.mark.asyncio
async def test_fresh_stdio_server_exposes_independent_review_cycle(tmp_path):
    transport = StdioTransport(command=sys.executable, args=["-m", "qa_orchestrator"],
        env={"QA_ORCHESTRATOR_DATA_DIR": str(tmp_path),
             "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
    async with Client(transport) as client:
        route = await client.call_tool("prepare_qa_orchestration", {"agent_profile": "code_reviewer"})
        constraints = " ".join(route.structured_content["constraints"])
        assert "independent second review" in constraints
        assert "independent verifier" in constraints


@pytest.mark.asyncio
async def test_stdio_bound_review_survives_restart(
    tmp_path, bound_review_context, review_finding_candidate,
    make_profile_review_report, make_final_review_report,
):
    manifest = bound_review_context.input_manifest.model_copy(update={
        "schema_version": 2,
        "source_inventory_status": "complete",
        "source_conflict_status": "clear",
    })
    context = bound_review_context.model_copy(update={
        "input_manifest": manifest,
        "evidence_registry": (
            EvidenceRecord(
                evidence_ref="E1", kind="source", revision=bound_review_context.head_sha,
                path="src/service.py", start_line=42, end_line=42,
                sha256="4" * 64, source_text_available=True,
            ),
            EvidenceRecord(evidence_ref="E2", kind="other", locator="host:context", sha256="5" * 64),
        ),
    })
    profile = make_profile_review_report(
        ReviewAgent.CODE_REVIEWER, findings=(review_finding_candidate,), context=context,
    )
    metrics = profile.execution_metrics
    attempt = metrics.attempts[0]
    receipt = {
        "prompt_write_status": "written", "submitted_prompt_sha256": attempt.submitted_prompt_sha256,
        "source_packet_sha256": context.evidence_packet_sha256,
        "selected_packet_sha256": profile.context_selection.selected_packet_sha256,
        "included_evidence_refs": profile.context_selection.included_refs,
        "omitted_evidence_refs": profile.context_selection.omitted_refs,
    }
    cycle_receipts = (
        {"stage": "review", **receipt}, {"stage": "second_look", **receipt},
        {"stage": "verify", **receipt, "selected_packet_sha256": context.evidence_packet_sha256,
         "included_evidence_refs": context.evidence_refs, "omitted_evidence_refs": ()},
    )
    attempt = type(attempt).model_validate({**attempt.model_dump(mode="python"),
        "review_cycle_receipts": cycle_receipts})
    profile = profile.model_copy(update={"execution_metrics": metrics.model_copy(update={"attempts": (attempt,)})})
    final = make_final_review_report(
        (ReviewAgent.CODE_REVIEWER,),
        findings=((review_finding_candidate, (ReviewAgent.CODE_REVIEWER,)),), context=context,
    )
    env = {
        "QA_ORCHESTRATOR_DATA_DIR": str(tmp_path),
        "QA_ORCHESTRATOR_SESSION_STORE_PATH": str(tmp_path / "sessions.sqlite3"),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
    }

    def transport():
        return StdioTransport(command=sys.executable, args=["-m", "qa_orchestrator"], env=env)

    async with Client(transport()) as client:
        started = await client.call_tool("start_qa_orchestration", {
            "task_type": "ordinary_review", "review_context": context.model_dump(mode="json"),
        })
        run_id = started.structured_content["run_id"]
        triage_request = {
            "run_id": run_id, "completed_step": "triage", "status": "completed",
            "selected_profile": "code_reviewer", "request_id": "triage-1", "expected_revision": 0,
        }
        await client.call_tool("advance_qa_orchestration", triage_request)
        advanced = await client.call_tool("advance_qa_orchestration", {
            "run_id": run_id, "completed_step": "primary_review", "status": "completed",
            "completed_profile": "code_reviewer", "profile_reports": [profile.model_dump(mode="json")],
            "risk_signals": {},
        })
        assert advanced.structured_content["current_step"] == "synthesis"

    async with Client(transport()) as client:
        recovered = await client.call_tool("get_qa_orchestration", {"run_id": run_id})
        assert recovered.structured_content["current_step"] == "synthesis"
        assert recovered.structured_content["profile_reports"][0]["profile"] == "code_reviewer"
        assert len(recovered.structured_content["profile_reports"][0]["execution_metrics"]["attempts"][0]["review_cycle_receipts"]) == 3
        # Simulate a lost triage response: replay the exact request after restart and
        # verify that it neither reapplies the transition nor discards later progress.
        replayed = await client.call_tool("advance_qa_orchestration", triage_request)
        assert replayed.structured_content["revision"] == recovered.structured_content["revision"]
        assert replayed.structured_content["current_step"] == "synthesis"
        await client.call_tool("advance_qa_orchestration", {
            "run_id": run_id, "completed_step": "synthesis", "status": "completed",
        })
        finished = await client.call_tool("finish_qa_orchestration", {
            "run_id": run_id, "outcome": "completed", "final_review_report": final.model_dump(mode="json"),
        })
        assert finished.structured_content["status"] == "completed"

    async with Client(transport()) as client:
        recovered = await client.call_tool("get_qa_orchestration", {"run_id": run_id})
        assert recovered.structured_content["status"] == "completed"
        assert recovered.structured_content["final_review_report"] is not None
