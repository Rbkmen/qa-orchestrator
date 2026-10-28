"""Exercise the installed MCP protocol across a real server restart."""

import sys

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from qa_orchestrator.contracts import EvidenceRecord, ReviewAgent


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
    final = make_final_review_report(
        (ReviewAgent.CODE_REVIEWER,),
        findings=((review_finding_candidate, (ReviewAgent.CODE_REVIEWER,)),), context=context,
    )
    env = {
        "QA_ORCHESTRATOR_DATA_DIR": str(tmp_path),
        "QA_ORCHESTRATOR_SESSION_STORE_PATH": str(tmp_path / "sessions.sqlite3"),
    }

    def transport():
        return StdioTransport(command=sys.executable, args=["-m", "qa_orchestrator"], env=env)

    async with Client(transport()) as client:
        started = await client.call_tool("start_qa_orchestration", {
            "task_type": "ordinary_review", "review_context": context.model_dump(mode="json"),
        })
        run_id = started.structured_content["run_id"]
        await client.call_tool("advance_qa_orchestration", {
            "run_id": run_id, "completed_step": "triage", "status": "completed",
            "selected_profile": "code_reviewer",
        })
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
