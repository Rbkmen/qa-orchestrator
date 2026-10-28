import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from qa_orchestrator.contracts import EvidenceRecord, ReviewAgent, ReviewBundle
from qa_orchestrator.review_profiles import bundle_execution_groups
from qa_orchestrator.server import build_server
from qa_orchestrator.service import OrchestratorService


def _manifest_v2(context):
    manifest = context.input_manifest.model_copy(
        update={
            "schema_version": 2,
            "source_inventory_status": "complete",
            "source_conflict_status": "clear",
        }
    )
    return context.model_copy(update={"input_manifest": manifest})


@pytest.mark.asyncio
async def test_server_exposes_ten_tools(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    async with Client(build_server(service)) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    assert set(tools) == {
        "prepare_qa_orchestration",
        "prepare_qa_orchestration_bundle",
        "get_qa_orchestration_model_policy",
        "finish_qa_orchestration",
        "start_qa_orchestration",
        "advance_qa_orchestration",
        "get_qa_orchestration",
        "list_qa_orchestrations",
        "get_qa_orchestration_catalog",
        "delete_qa_orchestration",
        "renew_qa_orchestration",
    }


@pytest.mark.asyncio
async def test_server_initialization_includes_advisory_trust_boundary(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    async with Client(build_server(service)) as client:
        initialization = await client.initialize()

    instructions = initialization.instructions
    assert instructions
    assert "advisory" in instructions.lower()
    assert "cannot confirm" in instructions.lower()
    assert "each review ran" in instructions.lower()
    assert "host_default" in instructions
    assert "one profile" in instructions.lower()
    assert "several changed files alone" in instructions.lower()
    assert "execution_groups" in instructions
    assert "completed_profiles" in instructions
    assert "selected_review_routes" in instructions
    assert "one call" in instructions.lower()
    assert "concurrently" in instructions.lower()
    assert "whether profiles ran concurrently" in instructions.lower()


@pytest.mark.asyncio
async def test_advance_tool_documents_host_reported_transition_limits(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    async with Client(build_server(service)) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    description = tools["advance_qa_orchestration"].description
    assert "host-reported" in description
    assert "cannot confirm" in description
    assert "risk_signals reflect evidence" in description
    assert "current_step" in description
    assert "status=completed" in description
    assert "never retry" in description.lower()
    assert "if the session is unknown or expired" in description.lower()
    assert "selected_review_routes" in description
    completed_profile = tools["advance_qa_orchestration"].inputSchema["properties"][
        "completed_profile"
    ]["description"]
    assert "fixed route order" in completed_profile
    assert "never advance parallel profiles concurrently" in completed_profile
    completed_profiles = tools["advance_qa_orchestration"].inputSchema["properties"][
        "completed_profiles"
    ]
    assert any(option.get("type") == "array" for option in completed_profiles.get("anyOf", []))
    assert "whole next execution_group" in completed_profiles["description"]


@pytest.mark.asyncio
async def test_server_publishes_tool_descriptions(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    async with Client(build_server(service)) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    assert all(tool.description and tool.description.strip() for tool in tools.values())
    for name, tool in tools.items():
        assert all(
            property_schema.get("description", "").strip()
            for property_schema in tool.inputSchema["properties"].values()
        ), name
    prepare_description = " ".join(tools["prepare_qa_orchestration"].description.split())
    assert "selected_review_routes" in prepare_description
    assert "do not make a separate preparation call" in prepare_description
    assert "standalone bundle lookup" in tools["prepare_qa_orchestration_bundle"].description
    start_context_description = tools["start_qa_orchestration"].inputSchema["properties"][
        "review_context"
    ]["description"]
    assert "Required for ordinary_review and widget_review" in start_context_description
    assert "schema v2" in start_context_description


@pytest.mark.asyncio
@pytest.mark.parametrize("task_type", ["ordinary_review", "widget_review"])
async def test_code_review_start_requires_bound_context_and_manifest_v2(
    tmp_path, bound_review_context, task_type
):
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    async with Client(build_server(service)) as client:
        with pytest.raises(ToolError, match="require a bound review_context"):
            await client.call_tool("start_qa_orchestration", {"task_type": task_type})

        with pytest.raises(ToolError, match="input_manifest schema_version=2"):
            await client.call_tool(
                "start_qa_orchestration",
                {
                    "task_type": task_type,
                    "review_context": bound_review_context.model_dump(mode="json"),
                },
            )

        started = await client.call_tool(
            "start_qa_orchestration",
            {
                "task_type": task_type,
                "review_context": _manifest_v2(bound_review_context).model_dump(mode="json"),
            },
        )

    assert started.structured_content["review_completion_mode"] == "strict"


@pytest.mark.asyncio
async def test_server_publishes_tool_annotations_and_schemas(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    async with Client(build_server(service)) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    advance_properties = tools["advance_qa_orchestration"].inputSchema["properties"]
    assert {"needs_deep_analysis", "reason_code"}.isdisjoint(advance_properties)
    assert tools["advance_qa_orchestration"].inputSchema["properties"]["completed_step"][
        "enum"
    ] == [
        "triage",
        "primary_review",
        "deep_review",
        "synthesis",
    ]
    assert {"run_id", "completed_step", "status"}.issubset(
        tools["advance_qa_orchestration"].inputSchema["required"]
    )
    assert "skip_synthesis" in advance_properties
    assert "no findings" in advance_properties["skip_synthesis"]["description"].lower()
    assert "omission_auditor" in advance_properties["implementation_review"]["description"]
    advance_output = tools["advance_qa_orchestration"].outputSchema
    assert advance_output is not None
    selected_routes_schema = advance_output["properties"]["selected_review_routes"]
    route_selection_schema = next(
        item for item in selected_routes_schema["anyOf"] if item.get("type") != "null"
    )
    assert "routes" in route_selection_schema["properties"]
    assert "execution_groups" in route_selection_schema["properties"]
    batch_schema = tools["prepare_qa_orchestration_bundle"].inputSchema
    assert set(batch_schema["properties"]["bundle"]["enum"]) == {
        bundle.value for bundle in ReviewBundle
    }
    assert tools["prepare_qa_orchestration_bundle"].outputSchema is not None
    groups_schema = tools["prepare_qa_orchestration_bundle"].outputSchema[
        "properties"
    ]["execution_groups"]
    assert "concurrently" in groups_schema["description"].lower()

    for name in (
        "prepare_qa_orchestration",
        "prepare_qa_orchestration_bundle",
        "get_qa_orchestration_model_policy",
        "get_qa_orchestration",
        "list_qa_orchestrations",
        "get_qa_orchestration_catalog",
    ):
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.readOnlyHint is True
        assert annotations.idempotentHint is True
        assert annotations.openWorldHint is False

    policy_tool = tools["get_qa_orchestration_model_policy"]
    assert policy_tool.inputSchema["properties"] == {}
    policy_output = policy_tool.outputSchema
    assert policy_output is not None
    mode_schema = policy_output.get("properties", {}).get("mode")
    assert mode_schema is not None
    assert set(mode_schema.get("enum", [])) == {"configured", "host_default"}
    assert tools["list_qa_orchestrations"].inputSchema["properties"] == {}
    assert tools["list_qa_orchestrations"].outputSchema is not None
    assert tools["get_qa_orchestration_catalog"].inputSchema["properties"] == {}
    assert tools["get_qa_orchestration_catalog"].outputSchema is not None

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
    deletion = tools["delete_qa_orchestration"]
    assert deletion.annotations.readOnlyHint is False
    assert deletion.annotations.destructiveHint is True
    assert deletion.annotations.idempotentHint is True
    assert deletion.annotations.openWorldHint is False
    assert set(deletion.inputSchema["properties"]) == {"run_id"}
    assert deletion.inputSchema["required"] == ["run_id"]
    assert deletion.outputSchema is not None

    run_id_schema = tools["get_qa_orchestration"].inputSchema["properties"]["run_id"]
    assert run_id_schema["pattern"] == r"^qar-[0-9a-f]{32}$"
    assert "returned by start_qa_orchestration" in run_id_schema["description"]
    assert set(tools["finish_qa_orchestration"].inputSchema["properties"]) == {
        "outcome",
        "run_id",
        "final_review_report",
        "expected_revision",
    }
    assert deletion.inputSchema["properties"]["run_id"]["pattern"] == run_id_schema["pattern"]


@pytest.mark.asyncio
async def test_tools_support_keyed_retries_and_active_lease_renewal(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    async with Client(build_server(service)) as client:
        started = await client.call_tool("start_qa_orchestration", {"task_type": "other"})
        payload = {
            "run_id": started.structured_content["run_id"], "completed_step": "triage",
            "status": "completed", "selected_profile": "python_reviewer",
            "request_id": "triage-1", "expected_revision": 0,
        }
        first = await client.call_tool("advance_qa_orchestration", payload)
        renewed = await client.call_tool("renew_qa_orchestration", {
            "run_id": payload["run_id"], "expected_revision": 1,
        })
        replay = await client.call_tool("advance_qa_orchestration", payload)
    assert first.structured_content["revision"] == renewed.structured_content["revision"] == 1
    assert replay.structured_content["expires_at"] == renewed.structured_content["expires_at"]
    assert replay.structured_content["revision"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["active", "early_stop", "awaiting_host_outcome", "finalized"])
async def test_delete_session_removes_only_requested_run_and_is_idempotent(tmp_path, state):
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    async with Client(build_server(service)) as client:
        target = (await client.call_tool(
            "start_qa_orchestration", {"task_type": "other"}
        )).structured_content
        retained = (await client.call_tool(
            "start_qa_orchestration", {"task_type": "other"}
        )).structured_content
        run_id = target["run_id"]
        if state == "early_stop":
            await client.call_tool("advance_qa_orchestration", {
                "run_id": run_id, "completed_step": "triage", "status": "partial"
            })
        elif state in {"awaiting_host_outcome", "finalized"}:
            await client.call_tool("advance_qa_orchestration", {
                "run_id": run_id, "completed_step": "triage", "status": "completed",
                "selected_profile": "code_reviewer"
            })
            await client.call_tool("advance_qa_orchestration", {
                "run_id": run_id, "completed_step": "primary_review", "status": "completed",
                "completed_profile": "code_reviewer", "risk_signals": {}
            })
            await client.call_tool("advance_qa_orchestration", {
                "run_id": run_id, "completed_step": "synthesis", "status": "completed"
            })
            if state == "finalized":
                await client.call_tool("finish_qa_orchestration", {
                    "run_id": run_id, "outcome": "completed"
                })

        first = await client.call_tool("delete_qa_orchestration", {"run_id": run_id})
        repeated = await client.call_tool("delete_qa_orchestration", {"run_id": run_id})
        assert first.structured_content == {"run_id": run_id, "deleted": True}
        assert repeated.structured_content == first.structured_content
        listed = (await client.call_tool("list_qa_orchestrations", {})).structured_content
        assert [entry["run_id"] for entry in listed["sessions"]] == [retained["run_id"]]
        current = await client.call_tool("get_qa_orchestration", {"run_id": retained["run_id"]})
        assert current.structured_content == retained
        for name, extra in (
            ("get_qa_orchestration", {}),
            ("advance_qa_orchestration", {"completed_step": "triage", "status": "partial"}),
            ("finish_qa_orchestration", {"outcome": "partial"}),
        ):
            with pytest.raises(ToolError, match="unknown run_id"):
                await client.call_tool(name, {"run_id": run_id, **extra})


@pytest.mark.asyncio
async def test_delete_unknown_id_succeeds_and_invalid_input_preserves_sessions(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    async with Client(build_server(service)) as client:
        target = (await client.call_tool(
            "start_qa_orchestration", {"task_type": "other"}
        )).structured_content
        unknown = "qar-" + "0" * 32
        result = await client.call_tool("delete_qa_orchestration", {"run_id": unknown})
        assert result.structured_content == {"run_id": unknown, "deleted": True}
        for arguments in ({}, {"run_id": "invalid"}):
            with pytest.raises(ToolError):
                await client.call_tool("delete_qa_orchestration", arguments)
        assert (await client.call_tool(
            "get_qa_orchestration", {"run_id": target["run_id"]}
        )).structured_content == target


@pytest.mark.asyncio
async def test_prepare_qa_orchestration_returns_selected_profile(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    async with Client(build_server(service)) as client:
        result = await client.call_tool(
            "prepare_qa_orchestration",
            {"agent_profile": "security_reviewer"},
        )

    assert result.structured_content["profile"] == "security_reviewer"
    assert result.structured_content["display_name"] == "Security Reviewer"
    assert result.structured_content["read_only"] is True
    assert result.structured_content["host_owns_decisions"] is True


@pytest.mark.asyncio
async def test_prepare_qa_orchestration_bundle_returns_routes_and_execution_groups_without_session(
    tmp_path,
):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    async with Client(build_server(service)) as client:
        result = await client.call_tool(
            "prepare_qa_orchestration_bundle",
            {"bundle": "ruby_backend"},
        )
        sessions = await client.call_tool("list_qa_orchestrations", {})
        with pytest.raises(ToolError):
            await client.call_tool(
                "prepare_qa_orchestration_bundle",
                {"bundle": "custom"},
            )

    assert result.structured_content["bundle"] == "ruby_backend"
    assert [route["profile"] for route in result.structured_content["routes"]] == [
        "code_explorer",
        "ruby_reviewer",
        "pr_test_analyzer",
        "omission_auditor",
    ]
    assert result.structured_content["execution_groups"] == [
        ["code_explorer"],
        ["ruby_reviewer", "pr_test_analyzer"],
        ["omission_auditor"],
    ]
    assert sessions.structured_content["sessions"] == []


@pytest.mark.asyncio
async def test_catalog_discovers_profiles_and_ordered_bundles_without_a_session(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    async with Client(build_server(service)) as client:
        catalog = (await client.call_tool("get_qa_orchestration_catalog", {})).structured_content
        sessions = (await client.call_tool("list_qa_orchestrations", {})).structured_content
        profiles = {entry["profile"]: entry for entry in catalog["profiles"]}
        bundles = {entry["bundle"]: entry for entry in catalog["bundles"]}
        prepared = await client.call_tool(
            "prepare_qa_orchestration", {"agent_profile": "python_reviewer"}
        )

    assert sessions["sessions"] == []
    assert set(profiles) == {profile.value for profile in ReviewAgent}
    assert set(bundles) == {bundle.value for bundle in ReviewBundle}
    assert profiles["python_reviewer"]["display_name"] == "Python Reviewer"
    assert profiles["python_reviewer"]["focus"] == prepared.structured_content["focus"]
    assert all(entry["display_name"] and entry["focus"] for entry in profiles.values())
    assert bundles["python_backend"]["profiles"] == [
        "code_explorer", "python_reviewer", "pr_test_analyzer", "omission_auditor"
    ]
    assert bundles["widget_js"]["profiles"] == [
        "code_explorer", "code_reviewer", "react_reviewer", "pr_test_analyzer",
        "omission_auditor",
    ]
    assert bundles["ordinary_mr"]["execution_groups"] == [
        ["code_explorer"],
        ["code_reviewer", "pr_test_analyzer"],
        ["omission_auditor"],
    ]
    assert bundles["python_backend"]["execution_groups"] == [
        ["code_explorer", "python_reviewer", "pr_test_analyzer"],
        ["omission_auditor"],
    ]
    assert bundles["requirements"]["execution_groups"] == [
        ["code_explorer"],
        ["code_reviewer"],
        ["omission_auditor"],
    ]
    for entry in bundles.values():
        assert [profile for group in entry["execution_groups"] for profile in group] == (
            entry["profiles"]
        )
    assert all(entry["when_to_use"] for entry in bundles.values())
    assert all(set(entry["profiles"]).issubset(profiles) for entry in bundles.values())
    assert catalog["recommended_bundles_by_task_type"] == {
        "ordinary_review": ["ordinary_mr"],
        "widget_review": ["widget", "widget_js"],
        "epic_analysis": ["requirements"],
        "requirements_analysis": ["requirements"],
        "qa_planning": ["requirements"],
        "autotest_implementation": ["autotest", "ordinary_mr"],
        "other": [],
    }
    assert catalog["read_only"] is True
    assert catalog["host_owns_decisions"] is True


@pytest.mark.asyncio
async def test_catalog_read_preserves_session_state_and_persisted_bytes(tmp_path):
    from qa_orchestrator.config import Settings

    store_path = tmp_path / "sessions.sqlite3"
    service = OrchestratorService(Settings(orchestration_session_store_path=store_path))
    async with Client(build_server(service)) as client:
        started = (await client.call_tool(
            "start_qa_orchestration", {"task_type": "other"}
        )).structured_content
        before = store_path.read_bytes()
        first = await client.call_tool("get_qa_orchestration_catalog", {})
        second = await client.call_tool("get_qa_orchestration_catalog", {})
        current = await client.call_tool("get_qa_orchestration", {"run_id": started["run_id"]})

    assert current.structured_content == started
    assert first.structured_content == second.structured_content
    assert store_path.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("bundle", list(ReviewBundle))
async def test_catalog_bundle_route_is_accepted_by_triage(tmp_path, bundle):
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    async with Client(build_server(service)) as client:
        catalog = (await client.call_tool("get_qa_orchestration_catalog", {})).structured_content
        discovered = next(entry for entry in catalog["bundles"] if entry["bundle"] == bundle)
        started = (await client.call_tool(
            "start_qa_orchestration", {"task_type": "other"}
        )).structured_content
        routed = await client.call_tool("advance_qa_orchestration", {
            "run_id": started["run_id"],
            "completed_step": "triage",
            "status": "completed",
            "selected_bundle": discovered["bundle"],
        })

    assert routed.structured_content["review_profiles"] == discovered["profiles"]
    assert routed.structured_content["current_profile"] == discovered["profiles"][0]


@pytest.mark.asyncio
async def test_get_qa_orchestration_model_policy_returns_loaded_policy(tmp_path):
    loaded_policy = {
        "provider": "openai",
        "triage_model": "gpt-6-luna",
        "primary_model": "gpt-6.1-sol",
        "deep_model": "gpt-6.1-sol",
        "synthesis_model": "gpt-6.1-sol",
        "triage_reasoning": "max",
        "primary_reasoning": "medium",
        "deep_reasoning": "high",
        "synthesis_reasoning": "medium",
    }
    model_policy_path = tmp_path / "model-policy.json"
    model_policy_path.write_text(json.dumps(loaded_policy), encoding="utf-8")
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    model_policy_path.write_text("invalid json", encoding="utf-8")

    async with Client(build_server(service)) as client:
        result = await client.call_tool("get_qa_orchestration_model_policy", {})

    assert result.structured_content == {"mode": "configured", **loaded_policy}


@pytest.mark.asyncio
async def test_model_policy_tool_reports_host_default_when_unconfigured(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    async with Client(build_server(service)) as client:
        result = await client.call_tool("get_qa_orchestration_model_policy", {})

    assert result.structured_content == {"mode": "host_default"}


@pytest.mark.asyncio
async def test_list_qa_orchestrations_recovers_run_id_without_changing_state(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    async with Client(build_server(service)) as client:
        empty = await client.call_tool("list_qa_orchestrations", {})
        assert empty.structured_content["sessions"] == []
        started = await client.call_tool(
            "start_qa_orchestration", {"task_type": "other"}
        )
        listed = await client.call_tool("list_qa_orchestrations", {})
        entries = listed.structured_content["sessions"]
        assert len(entries) == 1
        assert entries[0] == {
            "run_id": started.structured_content["run_id"],
            "task_type": "other",
            "status": "active",
            "current_step": "triage",
            "selected_bundle": None,
            "selected_profile": None,
            "current_profile": None,
            "review_repository": None,
            "review_head_sha": None,
            "review_diff_sha256": None,
            "has_review_artifacts": False,
            "expires_at": started.structured_content["expires_at"],
        }
        recovered = await client.call_tool(
            "get_qa_orchestration", {"run_id": entries[0]["run_id"]}
        )

    assert recovered.structured_content == started.structured_content
    assert listed.structured_content["read_only"] is True
    assert listed.structured_content["host_owns_decisions"] is True


@pytest.mark.asyncio
async def test_orchestration_tools_return_no_evidence_fields(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    async with Client(build_server(service)) as client:
        result = await client.call_tool(
            "start_qa_orchestration",
            {"task_type": "other"},
        )

    payload = result.structured_content
    assert payload["model_policy"] == {"mode": "host_default"}
    assert payload["read_only"] is True
    assert payload["host_owns_decisions"] is True
    assert payload["allowed_profiles"] == [profile.value for profile in ReviewAgent]
    assert payload["allowed_bundles"] == [bundle.value for bundle in ReviewBundle]
    assert payload["recommended_bundles"] == []
    assert payload["recommended_profiles"] == []
    assert payload["selected_bundle"] is None
    assert payload["review_profiles"] == []
    assert "evidence" not in payload
    assert "prompt" not in payload
    assert "output" not in payload


@pytest.mark.asyncio
async def test_triage_scope_guard_is_exposed_via_mcp(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    async with Client(build_server(service)) as client:
        started = await client.call_tool("start_qa_orchestration", {"task_type": "other"})
        args = {"run_id": started.structured_content["run_id"], "completed_step": "triage",
                "status": "completed", "selected_profile": "pr_test_analyzer",
                "implementation_review": True}
        with pytest.raises(ToolError, match="cannot report implementation findings"):
            await client.call_tool("advance_qa_orchestration", args)
        current = await client.call_tool("get_qa_orchestration", {"run_id": args["run_id"]})
        assert current.structured_content["current_step"] == "triage"
        accepted = await client.call_tool("advance_qa_orchestration", {
            **args, "acknowledge_limited_profile": True,
        })
        assert "cannot report implementation findings" in accepted.structured_content["route_warning"]


@pytest.mark.asyncio
async def test_orchestration_tools_advance_and_get_structured_state(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    async with Client(build_server(service)) as client:
        started = await client.call_tool(
            "start_qa_orchestration",
            {"task_type": "other"},
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
@pytest.mark.parametrize("outcome", ["partial", "blocked"])
async def test_orchestration_tool_finalizes_early_stop(tmp_path, outcome):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    async with Client(build_server(service)) as client:
        started = await client.call_tool(
            "start_qa_orchestration",
            {"task_type": "other"},
        )
        run_id = started.structured_content["run_id"]
        with pytest.raises(ToolError):
            await client.call_tool(
                "finish_qa_orchestration",
                {"run_id": run_id, "outcome": outcome},
            )
        stopped = await client.call_tool(
            "advance_qa_orchestration",
            {"run_id": run_id, "completed_step": "triage", "status": outcome},
        )
        finalized = await client.call_tool(
            "finish_qa_orchestration",
            {"run_id": run_id, "outcome": outcome},
        )
        repeated = await client.call_tool(
            "finish_qa_orchestration",
            {"run_id": run_id, "outcome": outcome},
        )
        with pytest.raises(ToolError):
            await client.call_tool(
                "finish_qa_orchestration",
                {"run_id": run_id, "outcome": "completed"},
            )
        current = await client.call_tool("get_qa_orchestration", {"run_id": run_id})

    assert stopped.structured_content["status"] == outcome
    assert stopped.structured_content["current_step"] == "triage"
    assert finalized.structured_content["status"] == outcome
    assert finalized.structured_content["next_action"] != stopped.structured_content["next_action"]
    assert repeated.structured_content == finalized.structured_content
    assert current.structured_content == finalized.structured_content


@pytest.mark.asyncio
async def test_orchestration_tool_selects_sol_from_structured_risk_signals(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    async with Client(build_server(service)) as client:
        started = await client.call_tool(
            "start_qa_orchestration",
            {"task_type": "other"},
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
                    "rationale": "A material access-control question remains unresolved.",
                    "evidence_refs": ["E1"],
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
            {"task_type": "other"},
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
        "omission_auditor",
    ]
    assert advanced.structured_content["current_profile"] == "code_explorer"
    assert advanced.structured_content["completed_profiles"] == []
    selected_routes = advanced.structured_content["selected_review_routes"]
    assert [route["profile"] for route in selected_routes["routes"]] == [
        "code_explorer",
        "code_reviewer",
        "pr_test_analyzer",
        "omission_auditor",
    ]
    assert selected_routes["execution_groups"] == [
        [profile.value for profile in group]
        for group in bundle_execution_groups(ReviewBundle.ORDINARY_MR)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("bundle", list(ReviewBundle))
async def test_orchestration_tools_complete_each_bundle(tmp_path, bundle: ReviewBundle):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    async with Client(build_server(service)) as client:
        started = await client.call_tool(
            "start_qa_orchestration",
            {"task_type": "other"},
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

        groups = bundle_execution_groups(bundle)
        selected_routes = current.structured_content["selected_review_routes"]
        assert [route["profile"] for route in selected_routes["routes"]] == [
            profile.value for group in groups for profile in group
        ]
        assert selected_routes["execution_groups"] == [
            [profile.value for profile in group] for group in groups
        ]
        expected_routes = service.prepare_review_bundle(bundle).model_dump(mode="json")
        expected_routes.pop("bundle")
        assert selected_routes == expected_routes
        primary_advance_calls = 0
        for index, group in enumerate(groups):
            current = await client.call_tool(
                "advance_qa_orchestration",
                {
                    "run_id": run_id,
                    "completed_step": current.structured_content["current_step"],
                    "status": "completed",
                    "completed_profiles": [profile.value for profile in group],
                    "risk_signals": {} if index == len(groups) - 1 else None,
                },
            )
            primary_advance_calls += 1

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
    if bundle is ReviewBundle.ORDINARY_MR:
        assert primary_advance_calls == 3
        assert current.structured_content["completed_profiles"] == [
            "code_explorer",
            "code_reviewer",
            "pr_test_analyzer",
            "omission_auditor",
        ]


@pytest.mark.asyncio
async def test_bound_review_tools_record_context_and_structured_reports(
    tmp_path,
    bound_review_context,
    review_finding_candidate,
    make_profile_review_report,
    make_final_review_report,
):
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    review_context = _manifest_v2(bound_review_context).model_copy(
        update={
            "evidence_registry": (
                EvidenceRecord(
                    evidence_ref="E1",
                    kind="source",
                    revision=bound_review_context.head_sha,
                    path="src/service.py",
                    start_line=42,
                    end_line=42,
                    sha256="4" * 64,
                    source_text_available=True,
                ),
                EvidenceRecord(
                    evidence_ref="E2",
                    kind="other",
                    locator="host:context",
                    sha256="5" * 64,
                ),
            )
        }
    )
    profile_report = make_profile_review_report(
        ReviewAgent.CODE_REVIEWER,
        findings=(review_finding_candidate,),
        context=review_context,
    )
    final_report = make_final_review_report(
        (ReviewAgent.CODE_REVIEWER,),
        findings=((review_finding_candidate, (ReviewAgent.CODE_REVIEWER,)),),
        context=review_context,
    )

    async with Client(build_server(service)) as client:
        started = await client.call_tool(
            "start_qa_orchestration",
            {
                "task_type": "ordinary_review",
                "review_context": review_context.model_dump(mode="json"),
            },
        )
        run_id = started.structured_content["run_id"]
        assert started.structured_content["recommended_profiles"] == ["python_reviewer"]
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
                "profile_reports": [profile_report.model_dump(mode="json")],
                "risk_signals": {},
            },
        )
        awaiting_outcome = await client.call_tool(
            "advance_qa_orchestration",
            {
                "run_id": run_id,
                "completed_step": synthesis.structured_content["current_step"],
                "status": "completed",
            },
        )
        finished = await client.call_tool(
            "finish_qa_orchestration",
            {
                "run_id": run_id,
                "outcome": "completed",
                "final_review_report": final_report.model_dump(mode="json"),
            },
        )
        recovered = await client.call_tool("get_qa_orchestration", {"run_id": run_id})
        listed = await client.call_tool("list_qa_orchestrations", {})

    assert awaiting_outcome.structured_content["current_step"] == "awaiting_host_outcome"
    assert finished.structured_content["review_context"]["diff_sha256"] == "2" * 64
    assert finished.structured_content["final_review_report"]["finding_candidates"][0][
        "evidence_refs"
    ] == ["E1"]
    assert recovered.structured_content == finished.structured_content
    assert listed.structured_content["sessions"][0]["has_review_artifacts"] is True


@pytest.mark.asyncio
async def test_clean_single_profile_can_finalize_without_synthesis_tool_step(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    async with Client(build_server(service)) as client:
        started = await client.call_tool(
            "start_qa_orchestration",
            {"task_type": "other"},
        )
        run_id = started.structured_content["run_id"]
        triage = await client.call_tool(
            "advance_qa_orchestration",
            {
                "run_id": run_id,
                "completed_step": "triage",
                "status": "completed",
                "selected_profile": "code_reviewer",
            },
        )
        selected_routes = triage.structured_content["selected_review_routes"]
        assert selected_routes == {
            "routes": [
                service.prepare_review_route(ReviewAgent.CODE_REVIEWER).model_dump(mode="json")
            ],
            "execution_groups": [["code_reviewer"]],
        }
        advanced = await client.call_tool(
            "advance_qa_orchestration",
            {
                "run_id": run_id,
                "completed_step": "primary_review",
                "status": "completed",
                "completed_profile": "code_reviewer",
                "risk_signals": {},
                "skip_synthesis": True,
            },
        )
        final = await client.call_tool(
            "finish_qa_orchestration",
            {"run_id": run_id, "outcome": "completed"},
        )

    assert advanced.structured_content["status"] == "awaiting_host_outcome"
    assert advanced.structured_content["current_step"] == "awaiting_host_outcome"
    assert advanced.structured_content["model_policy"] is None
    assert final.structured_content["status"] == "completed"


@pytest.mark.asyncio
async def test_orchestration_tool_rejects_incompatible_bundle_signals(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    async with Client(build_server(service)) as client:
        started = await client.call_tool(
            "start_qa_orchestration",
            {"task_type": "other"},
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
            {"task_type": "other"},
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
            {"task_type": "other"},
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
            await client.call_tool("prepare_qa_orchestration", {"agent_profile": "unknown"})


@pytest.mark.asyncio
async def test_compact_advance_preserves_recovery_state_and_default_response(tmp_path):
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    async with Client(build_server(service)) as client:
        started = (await client.call_tool("start_qa_orchestration", {"task_type": "other"})).structured_content
        result = (await client.call_tool("advance_qa_orchestration", {
            "run_id": started["run_id"], "completed_step": "triage", "status": "completed",
            "selected_profile": "code_reviewer", "compact_response": True,
        })).structured_content
        assert result["current_step"] == "primary_review"
        assert result["selected_review_routes"]["routes"]
        for key in ("review_context", "profile_reports", "audit_events", "transition_receipts"):
            assert key not in result
        full = (await client.call_tool("get_qa_orchestration", {"run_id": started["run_id"]})).structured_content
        assert "audit_events" in full and "profile_reports" in full
        other = (await client.call_tool("start_qa_orchestration", {"task_type": "other"})).structured_content
        default = (await client.call_tool("advance_qa_orchestration", {
            "run_id": other["run_id"], "completed_step": "triage", "status": "completed",
            "selected_profile": "code_reviewer",
        })).structured_content
        assert "audit_events" in default and "profile_reports" in default
