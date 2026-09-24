import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import qa_orchestrator.server as server_module
from qa_orchestrator.contracts import ReviewBundle
from qa_orchestrator.review_profiles import REVIEW_BUNDLES
from qa_orchestrator.server import build_server
from qa_orchestrator.service import OrchestratorService


@pytest.mark.asyncio
async def test_server_exposes_six_tools(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

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
async def test_server_publishes_tool_annotations_and_schemas(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    async with Client(build_server(service)) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    advance_description = tools["advance_qa_orchestration"].description or ""
    assert "primary review" in advance_description.lower()
    assert "terra primary" not in advance_description.lower()
    risk_signals_description = tools["advance_qa_orchestration"].inputSchema["properties"][
        "risk_signals"
    ]["description"]
    assert "required with the final" in risk_signals_description.lower()
    assert tools["advance_qa_orchestration"].inputSchema["properties"]["completed_step"][
        "enum"
    ] == [
        "triage",
        "primary_review",
        "deep_review",
        "synthesis",
        "awaiting_host_outcome",
    ]

    for name in (
        "prepare_review_route",
        "get_qa_orchestration",
        "get_metrics_report",
    ):
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.readOnlyHint is True
        assert annotations.idempotentHint is True
        assert annotations.openWorldHint is False

    for name in (
        "start_qa_orchestration",
        "advance_qa_orchestration",
        "record_qa_task_outcome",
    ):
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.readOnlyHint is False
        assert annotations.openWorldHint is False

    run_id_schema = tools["get_qa_orchestration"].inputSchema["properties"]["run_id"]
    assert run_id_schema["pattern"] == r"^qar-[0-9a-f]{32}$"
    assert (
        tools["get_metrics_report"].inputSchema["properties"]["days"]["minimum"] == 1
    )
    assert set(tools["record_qa_task_outcome"].inputSchema["properties"]) == {
        "task_type",
        "outcome",
        "run_id",
    }

    report_schema = tools["get_metrics_report"].outputSchema
    assert set(report_schema["properties"]) == {
        "period_days",
        "total_tasks",
        "by_task_type",
    }
    assert report_schema["additionalProperties"] is False


@pytest.mark.asyncio
async def test_prepare_review_route_returns_selected_profile(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    async with Client(build_server(service)) as client:
        result = await client.call_tool(
            "prepare_review_route",
            {"agent_profile": "security_reviewer"},
        )

    assert result.structured_content["profile"] == "security_reviewer"
    assert result.structured_content["display_name"] == "Security Reviewer"
    assert result.structured_content["read_only"] is True
    assert result.structured_content["host_owns_decisions"] is True


@pytest.mark.asyncio
async def test_orchestration_tools_return_no_evidence_fields(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    async with Client(build_server(service)) as client:
        result = await client.call_tool(
            "start_qa_orchestration",
            {"task_type": "ordinary_review"},
        )

    payload = result.structured_content
    assert payload["model_policy"] == {
        "provider": "openai",
        "model": "gpt-6-luna",
        "reasoning": "max",
    }
    assert payload["read_only"] is True
    assert payload["host_owns_decisions"] is True
    assert payload["allowed_profiles"] == [
        "pr_test_analyzer",
        "code_reviewer",
        "security_reviewer",
        "silent_failure_hunter",
        "code_explorer",
        "typescript_reviewer",
        "react_reviewer",
    ]
    assert payload["allowed_bundles"] == [bundle.value for bundle in ReviewBundle]
    assert payload["selected_bundle"] is None
    assert payload["review_profiles"] == []
    assert "evidence" not in payload
    assert "prompt" not in payload
    assert "output" not in payload


@pytest.mark.asyncio
async def test_orchestration_tools_advance_and_get_structured_state(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

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

    assert advanced.structured_content["current_step"] == "primary_review"
    assert current.structured_content["current_step"] == "primary_review"
    assert current.structured_content["selected_profile"] == "code_reviewer"


def test_server_refuses_to_start_when_existing_data_cannot_be_sanitized(monkeypatch):
    monkeypatch.setattr(
        server_module.JsonEventSink,
        "sanitize_existing_records",
        lambda _self: False,
    )
    monkeypatch.setattr(
        server_module,
        "build_server",
        lambda _service: pytest.fail("server must not start with unsanitized data"),
    )

    with pytest.raises(SystemExit, match="could not be sanitized"):
        server_module.main()


@pytest.mark.asyncio
async def test_orchestration_tool_selects_sol_from_structured_risk_signals(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    async with Client(build_server(service)) as client:
        started = await client.call_tool(
            "start_qa_orchestration",
            {"task_type": "ordinary_review"},
        )
        run_id = started.structured_content["run_id"]
        primary = await client.call_tool(
            "advance_qa_orchestration",
            {
                "run_id": run_id,
                "completed_step": "triage",
                "status": "completed",
                "selected_profile": "code_reviewer",
            },
        )
        deep = await client.call_tool(
            "advance_qa_orchestration",
            {
                "run_id": run_id,
                "completed_step": primary.structured_content["current_step"],
                "status": "completed",
                "completed_profile": "code_reviewer",
                "risk_signals": {
                    "high_risk_domain": True,
                    "evidence_uncertain": True,
                },
            },
        )

    assert deep.structured_content["current_step"] == "deep_review"
    assert deep.structured_content["deep_assessment"] == {
        "should_escalate": True,
        "triggered_rules": ["high_risk_with_uncertainty"],
        "reason_codes": ["high_risk_domain", "evidence_gap"],
        "complexity_signal_count": 0,
    }


@pytest.mark.asyncio
async def test_orchestration_tools_advance_with_bundle_order(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    async with Client(build_server(service)) as client:
        started = await client.call_tool(
            "start_qa_orchestration",
            {"task_type": "ordinary_review"},
        )
        advanced = await client.call_tool(
            "advance_qa_orchestration",
            {
                "run_id": started.structured_content["run_id"],
                "completed_step": "triage",
                "status": "completed",
                "selected_bundle": "ordinary_mr",
            },
        )

    assert advanced.structured_content["selected_bundle"] == "ordinary_mr"
    assert advanced.structured_content["selected_profile"] == "code_explorer"
    assert advanced.structured_content["review_profiles"] == [
        "code_explorer",
        "code_reviewer",
        "pr_test_analyzer",
    ]
    assert advanced.structured_content["current_profile"] == "code_explorer"
    assert advanced.structured_content["completed_profiles"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bundle", list(ReviewBundle))
async def test_orchestration_tools_complete_each_bundle(tmp_path, bundle: ReviewBundle):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    async with Client(build_server(service)) as client:
        started = await client.call_tool(
            "start_qa_orchestration",
            {"task_type": "ordinary_review"},
        )
        run_id = started.structured_content["run_id"]
        current = await client.call_tool(
            "advance_qa_orchestration",
            {
                "run_id": run_id,
                "completed_step": "triage",
                "status": "completed",
                "selected_bundle": bundle.value,
            },
        )

        profiles = REVIEW_BUNDLES[bundle]
        for index, profile in enumerate(profiles):
            current = await client.call_tool(
                "advance_qa_orchestration",
                {
                    "run_id": run_id,
                    "completed_step": current.structured_content["current_step"],
                    "status": "completed",
                    "completed_profile": profile.value,
                    "risk_signals": {} if index == len(profiles) - 1 else None,
                },
            )

        synthesis = await client.call_tool(
            "advance_qa_orchestration",
            {
                "run_id": run_id,
                "completed_step": current.structured_content["current_step"],
                "status": "completed",
            },
        )
        outcome = await client.call_tool(
            "record_qa_task_outcome",
            {
                "task_type": "ordinary_review",
                "outcome": "completed",
                "run_id": run_id,
            },
        )
        final = await client.call_tool("get_qa_orchestration", {"run_id": run_id})

    assert synthesis.structured_content["status"] == "awaiting_host_outcome"
    assert outcome.structured_content == {"status": "recorded"}
    assert final.structured_content["status"] == "completed"


@pytest.mark.asyncio
async def test_orchestration_tool_rejects_incompatible_bundle_signals(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    async with Client(build_server(service)) as client:
        started = await client.call_tool(
            "start_qa_orchestration",
            {"task_type": "ordinary_review"},
        )
        run_id = started.structured_content["run_id"]

        with pytest.raises(ToolError):
            await client.call_tool(
                "advance_qa_orchestration",
                {
                    "run_id": run_id,
                    "completed_step": "triage",
                    "status": "completed",
                    "selected_profile": "code_reviewer",
                    "selected_bundle": "ordinary_mr",
                },
            )

        with pytest.raises(ToolError):
            await client.call_tool(
                "advance_qa_orchestration",
                {
                    "run_id": run_id,
                    "completed_step": "triage",
                    "status": "completed",
                },
            )

        with pytest.raises(ToolError):
            await client.call_tool(
                "advance_qa_orchestration",
                {
                    "run_id": run_id,
                    "completed_step": "triage",
                    "status": "completed",
                    "selected_bundle": "unknown_bundle",
                },
            )


@pytest.mark.asyncio
async def test_orchestration_tool_rejects_selection_after_luna_and_custom_order(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    async with Client(build_server(service)) as client:
        started = await client.call_tool(
            "start_qa_orchestration",
            {"task_type": "ordinary_review"},
        )
        run_id = started.structured_content["run_id"]
        await client.call_tool(
            "advance_qa_orchestration",
            {
                "run_id": run_id,
                "completed_step": "triage",
                "status": "completed",
                "selected_bundle": "ordinary_mr",
            },
        )

        with pytest.raises(ToolError):
            await client.call_tool(
                "advance_qa_orchestration",
                {
                    "run_id": run_id,
                    "completed_step": "primary_review",
                    "status": "completed",
                    "selected_profile": "security_reviewer",
                },
            )

        with pytest.raises(ToolError):
            await client.call_tool(
                "advance_qa_orchestration",
                {
                    "run_id": run_id,
                    "completed_step": "primary_review",
                    "status": "completed",
                    "review_profiles": ["code_reviewer", "code_explorer"],
                },
            )


@pytest.mark.asyncio
async def test_server_records_orchestration_metrics(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    async with Client(build_server(service)) as client:
        started = await client.call_tool(
            "start_qa_orchestration",
            {"task_type": "ordinary_review"},
        )
        run_id = started.structured_content["run_id"]
        primary = await client.call_tool(
            "advance_qa_orchestration",
            {
                "run_id": run_id,
                "completed_step": "triage",
                "status": "completed",
                "selected_profile": "code_reviewer",
            },
        )
        synthesis = await client.call_tool(
            "advance_qa_orchestration",
            {
                "run_id": run_id,
                "completed_step": primary.structured_content["current_step"],
                "status": "completed",
                "completed_profile": "code_reviewer",
                "risk_signals": {},
            },
        )
        await client.call_tool(
            "advance_qa_orchestration",
            {
                "run_id": run_id,
                "completed_step": synthesis.structured_content["current_step"],
                "status": "completed",
            },
        )
        await client.call_tool(
            "record_qa_task_outcome",
            {
                "task_type": "ordinary_review",
                "outcome": "completed",
                "run_id": run_id,
            },
        )
        report = await client.call_tool("get_metrics_report", {"days": 7})
        current = await client.call_tool("get_qa_orchestration", {"run_id": run_id})

    assert report.structured_content == {
        "period_days": 7,
        "total_tasks": 1,
        "by_task_type": {"ordinary_review": 1},
    }
    assert current.structured_content["status"] == "completed"


@pytest.mark.asyncio
async def test_task_outcome_and_report_only_expose_task_distribution(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    async with Client(build_server(service)) as client:
        outcome = await client.call_tool(
            "record_qa_task_outcome",
            {
                "task_type": "ordinary_review",
                "outcome": "completed",
            },
        )
        report = await client.call_tool("get_metrics_report", {"days": 7})

    assert outcome.structured_content == {"status": "recorded"}
    assert report.structured_content == {
        "period_days": 7,
        "total_tasks": 1,
        "by_task_type": {"ordinary_review": 1},
    }


@pytest.mark.asyncio
async def test_metrics_report_rejects_non_positive_days(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    async with Client(build_server(service)) as client:
        with pytest.raises(ToolError, match="days must be positive"):
            await client.call_tool("get_metrics_report", {"days": 0})


@pytest.mark.asyncio
async def test_unknown_profile_is_rejected_by_mcp_schema(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    async with Client(build_server(service)) as client:
        with pytest.raises(ToolError):
            await client.call_tool("prepare_review_route", {"agent_profile": "unknown"})
