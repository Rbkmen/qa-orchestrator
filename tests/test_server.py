import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from qa_router_mcp.server import build_server
from qa_router_mcp.service import RouterService


@pytest.mark.asyncio
async def test_server_exposes_only_deterministic_route_and_metrics_tools(tmp_path):
    service = RouterService.from_settings(data_dir=tmp_path)

    async with Client(build_server(service)) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    assert set(tools) == {
        "prepare_review_route",
        "record_qa_task_outcome",
        "get_metrics_report",
    }
    assert "qwen_used" not in tools["record_qa_task_outcome"].inputSchema["properties"]
    assert "qwen_edits" not in tools["record_qa_task_outcome"].inputSchema["properties"]


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
    assert "qwen_tasks" not in report.structured_content["qa_tasks"]
    assert "generation_events" not in report.structured_content
    assert "qwen" not in json.dumps(report.structured_content).lower()
    assert "qwen" not in (tmp_path / "metrics.jsonl").read_text(encoding="utf-8").lower()


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
