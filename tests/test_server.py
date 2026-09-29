import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from qa_orchestrator.contracts import ReviewAgent, ReviewBundle
from qa_orchestrator.review_profiles import REVIEW_BUNDLES
from qa_orchestrator.server import build_server
from qa_orchestrator.service import OrchestratorService


@pytest.mark.asyncio
async def test_server_exposes_five_tools(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    async with Client(build_server(service)) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    assert set(tools) == {
        "prepare_review_route",
        "finish_qa_orchestration",
        "start_qa_orchestration",
        "advance_qa_orchestration",
        "get_qa_orchestration",
    }


@pytest.mark.asyncio
async def test_server_publishes_tool_annotations_and_schemas(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    async with Client(build_server(service)) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    advance_description = " ".join(
        (tools["advance_qa_orchestration"].description or "").split()
    )
    assert "primary review" in advance_description.lower()
    assert "out-of-order" in advance_description.lower()
    assert "current_step as completed_step" in advance_description
    assert "get_qa_orchestration" in advance_description
    assert "replaying the prior step is rejected" in advance_description
    assert "Completed triage requires exactly one" in advance_description
    assert "unknown or expired run_ids return errors" in advance_description
    assert "without advancing the run" in advance_description
    assert "omit both on an early stop" in advance_description
    advance_properties = tools["advance_qa_orchestration"].inputSchema["properties"]
    assert {"needs_deep_analysis", "reason_code"}.isdisjoint(advance_properties)
    for name, tool in tools.items():
        assert all(
            property_schema.get("description", "").strip()
            for property_schema in tool.inputSchema["properties"].values()
        ), name

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
    ]
    assert "finish_qa_orchestration" in tools["advance_qa_orchestration"].inputSchema[
        "properties"
    ]["completed_step"]["description"]

    task_type_description = tools["start_qa_orchestration"].inputSchema["properties"][
        "task_type"
    ]["description"]
    assert "recommended_bundles only" in task_type_description
    assert "risk level" in task_type_description

    prepare_description = " ".join((tools["prepare_review_route"].description or "").split())
    assert "fixed checklist for one `agent_profile`" in prepare_description
    assert "required output sections" in prepare_description
    assert "one scoped concern" in prepare_description
    assert "once per `review_profiles` member in session order" in prepare_description
    assert "never selects a session profile" in prepare_description
    assert "start_qa_orchestration" in prepare_description
    assert "stateless lookup" in prepare_description
    assert "does not inspect repository content" in prepare_description
    get_description = " ".join((tools["get_qa_orchestration"].description or "").split())
    assert "do not extend the TTL" in get_description
    assert "cannot be recovered here" in get_description
    start_description = tools["start_qa_orchestration"].description or ""
    assert "100 sessions by default" in start_description
    assert "session limit" in start_description
    finish_description = " ".join((tools["finish_qa_orchestration"].description or "").split())
    assert "Call only when" in finish_description
    assert "same `partial` or `blocked` outcome" in finish_description
    assert "returns the retained terminal session" in finish_description
    assert "conflicting final outcome" in finish_description
    assert "leaves the stored status unchanged" in finish_description
    assert "TTL expires" in finish_description
    assert "evicted to free capacity" in finish_description
    assert "service restarts" in finish_description
    outcome_description = tools["finish_qa_orchestration"].inputSchema["properties"]["outcome"][
        "description"
    ]
    assert "completed, partial, or blocked" in outcome_description
    assert "already used for an early stop" not in outcome_description

    for name in (
        "prepare_review_route",
        "get_qa_orchestration",
    ):
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.readOnlyHint is True
        assert annotations.idempotentHint is True
        assert annotations.openWorldHint is False

    for name in (
        "start_qa_orchestration",
        "advance_qa_orchestration",
        "finish_qa_orchestration",
    ):
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.readOnlyHint is False
        assert annotations.openWorldHint is False

    assert tools["start_qa_orchestration"].annotations.idempotentHint is False
    assert tools["advance_qa_orchestration"].annotations.idempotentHint is False
    assert tools["finish_qa_orchestration"].annotations.idempotentHint is True

    run_id_schema = tools["get_qa_orchestration"].inputSchema["properties"]["run_id"]
    assert run_id_schema["pattern"] == r"^qar-[0-9a-f]{32}$"
    assert "returned by start_qa_orchestration" in run_id_schema["description"]
    assert set(tools["finish_qa_orchestration"].inputSchema["properties"]) == {
        "outcome",
        "run_id",
    }


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
        "reasoning_capabilities_verified": True,
    }
    assert payload["read_only"] is True
    assert payload["host_owns_decisions"] is True
    assert payload["allowed_profiles"] == [profile.value for profile in ReviewAgent]
    assert payload["allowed_bundles"] == [bundle.value for bundle in ReviewBundle]
    assert payload["recommended_bundles"] == ["ordinary_mr"]
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
                "completed_step": "triage",
                "status": "completed",
                "selected_profile": "code_reviewer",
            },
        )
        current = await client.call_tool("get_qa_orchestration", {"run_id": run_id})

    assert advanced.structured_content["current_step"] == "primary_review"
    assert current.structured_content["current_step"] == "primary_review"
    assert current.structured_content["selected_profile"] == "code_reviewer"


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
            "finish_qa_orchestration",
            {
                "outcome": "completed",
                "run_id": run_id,
            },
        )
        final = await client.call_tool("get_qa_orchestration", {"run_id": run_id})

    assert synthesis.structured_content["status"] == "awaiting_host_outcome"
    assert outcome.structured_content["status"] == "completed"
    assert "outcome_recorded" not in outcome.structured_content
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
async def test_orchestration_tool_rejects_selection_after_triage_and_custom_order(tmp_path):
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
async def test_server_completes_full_flow_without_state_reads_or_task_persistence(tmp_path):
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
        after_primary = await client.call_tool(
            "advance_qa_orchestration",
            {
                "run_id": run_id,
                "completed_step": primary.structured_content["current_step"],
                "status": "completed",
                "completed_profile": "code_reviewer",
                "risk_signals": {},
            },
        )
        awaiting_host = await client.call_tool(
            "advance_qa_orchestration",
            {
                "run_id": run_id,
                "completed_step": after_primary.structured_content["current_step"],
                "status": "completed",
            },
        )
        final = await client.call_tool(
            "finish_qa_orchestration",
            {
                "outcome": "completed",
                "run_id": run_id,
            },
        )
    assert primary.structured_content["current_step"] == "primary_review"
    assert awaiting_host.structured_content["status"] == "awaiting_host_outcome"
    assert final.structured_content["status"] == "completed"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_unknown_profile_is_rejected_by_mcp_schema(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    async with Client(build_server(service)) as client:
        with pytest.raises(ToolError):
            await client.call_tool("prepare_review_route", {"agent_profile": "unknown"})
