import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from qa_router_mcp.server import build_server
from qa_router_mcp.service import RouterService


@pytest.mark.asyncio
async def test_server_exposes_six_tools(tmp_path):
    service = RouterService.from_settings(data_dir=tmp_path)

    async with Client(build_server(service)) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    assert set(tools) == {
        "prepare_review_route",
        "record_qa_task_outcome",
        "get_metrics_report",
        "start_qa_orchestration",
        "advance_qa_orchestration",
        "get_qa_orchestration",
    }


@pytest.mark.asyncio
async def test_prepare_review_route_returns_selected_profile(tmp_path):
    service = RouterService.from_settings(data_dir=tmp_path)

    async with Client(build_server(service)) as client:
        result = await client.call_tool(
            "prepare_review_route",
            {"agent_profile": "security_reviewer"},
        )

    assert result.structured_content["profile"] == "security_reviewer"
    assert result.structured_content["read_only"] is True
    assert result.structured_content["host_owns_decisions"] is True


@pytest.mark.asyncio
async def test_orchestration_tools_return_no_evidence_fields(tmp_path):
    service = RouterService.from_settings(data_dir=tmp_path)

    async with Client(build_server(service)) as client:
        result = await client.call_tool(
            "start_qa_orchestration",
            {"task_type": "ordinary_review"},
        )

    payload = result.structured_content
    assert payload["model_policy"] == {
        "model": "gpt-5.6-luna",
        "reasoning": "max",
    }
    assert payload["read_only"] is True
    assert payload["host_owns_decisions"] is True
    assert "evidence" not in payload
    assert "prompt" not in payload
    assert "output" not in payload


@pytest.mark.asyncio
async def test_orchestration_tools_advance_and_get_structured_state(tmp_path):
    service = RouterService.from_settings(data_dir=tmp_path)

    async with Client(build_server(service)) as client:
        started = await client.call_tool(
            "start_qa_orchestration",
            {"task_type": "ordinary_review"},
        )
        run_id = started.structured_content["run_id"]
        advanced = await client.call_tool(
            "advance_qa_orchestration",
            {
                "run_id": run_id,
                "completed_step": "luna_triage",
                "status": "completed",
                "selected_profile": "code_reviewer",
            },
        )
        current = await client.call_tool("get_qa_orchestration", {"run_id": run_id})

    assert advanced.structured_content["current_step"] == "terra_primary_review"
    assert current.structured_content["current_step"] == "terra_primary_review"
    assert current.structured_content["selected_profile"] == "code_reviewer"


@pytest.mark.asyncio
async def test_task_outcome_and_report_are_model_free(tmp_path):
    service = RouterService.from_settings(data_dir=tmp_path)

    async with Client(build_server(service)) as client:
        outcome = await client.call_tool(
            "record_qa_task_outcome",
            {
                "task_type": "ordinary_review",
                "outcome": "completed",
                "codegraph_calls": 1,
                "source_mcp_calls": 2,
                "findings_identified": 2,
                "findings_confirmed": 1,
                "findings_rejected": 1,
                "repeated_source_reads": 0,
            },
        )
        report = await client.call_tool("get_metrics_report", {"days": 7})

    assert outcome.structured_content == {"status": "recorded"}
    assert report.structured_content["qa_tasks"]["events"] == 1
    assert "generation_events" not in report.structured_content


@pytest.mark.asyncio
async def test_metrics_report_rejects_non_positive_days(tmp_path):
    service = RouterService.from_settings(data_dir=tmp_path)

    async with Client(build_server(service)) as client:
        with pytest.raises(ToolError, match="days must be positive"):
            await client.call_tool("get_metrics_report", {"days": 0})


@pytest.mark.asyncio
async def test_unknown_profile_is_rejected_by_mcp_schema(tmp_path):
    service = RouterService.from_settings(data_dir=tmp_path)

    async with Client(build_server(service)) as client:
        with pytest.raises(ToolError):
            await client.call_tool("prepare_review_route", {"agent_profile": "unknown"})
