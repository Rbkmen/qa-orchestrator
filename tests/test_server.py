import re

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from qa_router_mcp.config import Settings
from qa_router_mcp.contracts import DraftEnvelope
from qa_router_mcp.events import CANARY_TARGET
from qa_router_mcp.server import build_server
from qa_router_mcp.service import RouterService


class DraftFake:
    def __init__(self):
        self.prompts = []

    async def count_tokens(self, prompt):
        return 100

    async def generate(self, prompt, *, max_output_tokens=None, allow_schema_repair=True):
        self.prompts.append(prompt)
        if "PRESERVE_TERMS" in prompt:
            return DraftEnvelope(draft="Перевод checkout", unverified=["Review locally"])
        if "REVIEW_AGENT_PROFILE" in prompt:
            return DraftEnvelope(
                draft=(
                    "Scope: changed behavior\nChecklist: inspect changed branches\n"
                    "Candidate Coverage Gaps: error path\n"
                    "Positive Observations: bounded input\nUnverified: runtime evidence"
                ),
                unverified=["Review locally"],
            )
        return DraftEnvelope(
            draft=(
                "Coverage ID: COV-GUEST-HAPPY\nTitle: Case\n"
                "Preconditions: Ready\nSteps: 1. Act\n"
                "Expected Result: Expected behavior"
            ),
            unverified=["Review locally"],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [True, False])
async def test_numeric_coverage_ids_survive_full_tool_flow(tmp_path, legacy):
    class CoverageEcho(DraftFake):
        async def generate(self, prompt, **kwargs):
            self.prompts.append(prompt)
            ids = list(dict.fromkeys(re.findall(r"(?m)^Coverage ID: (COV-\d+)$", prompt)))
            return DraftEnvelope(
                draft="\n\n".join(
                    f"Coverage ID: {item}\nTitle: Case\nPreconditions: Ready\n"
                    "Steps: Open form\nExpected Result: Form is shown"
                    for item in ids
                )
                or "No coverage IDs"
            )

    drafting = CoverageEcho()
    coverage = (
        ["Open form", "Close form"]
        if legacy
        else [
            {
                "coverage_id": f"COV-{i:02d}",
                "purpose": "Open form",
                "source": "ABC-123",
                "state": "Ready",
                "expected_invariant": "Form shown",
            }
            for i in (1, 2)
        ]
    )
    async with Client(build_server(RouterService(Settings(data_dir=tmp_path), drafting))) as client:
        result = await client.call_tool(
            "draft_test_cases",
            {
                "requirement": "ABC-123 form behavior",
                "coverage_map": coverage,
            },
        )
    assert result.structured_content["status"] == "ok"
    assert len(drafting.prompts) == 1
    assert "ABC-123" not in drafting.prompts[0]
    assert "COV-01" in result.structured_content["draft"]
    assert "COV-02" in result.structured_content["draft"]


@pytest.mark.asyncio
async def test_server_exposes_drafting_and_metrics_tools(tmp_path):
    drafting = DraftFake()
    service = RouterService(Settings(data_dir=tmp_path), drafting)

    async with Client(build_server(service)) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        names = set(tools)
        assert names == {
            "draft_test_cases",
            "draft_review_checklist",
            "summarize_logs",
            "draft_automation_skeleton",
            "translate_text",
            "rewrite_text",
            "explain_short",
            "summarize_text",
            "record_canary_feedback",
            "record_qa_task_outcome",
            "get_metrics_report",
        }
        outcome_fields = tools["record_qa_task_outcome"].inputSchema["properties"]
        assert {
            "codegraph_response_tokens",
            "source_mcp_response_tokens",
            "avoided_source_read_tokens",
            "deep_analysis_used",
            "deep_model",
            "deep_reasoning",
            "deep_duration_ms",
            "deep_input_tokens",
            "deep_output_tokens",
        } <= outcome_fields.keys()

        result = await client.call_tool(
            "draft_test_cases",
            {
                "requirement": "Guest checkout",
                "coverage_map": [
                    {
                        "coverage_id": "COV-GUEST-HAPPY",
                        "purpose": "Happy path",
                        "source": "Confirmed requirement",
                        "state": "Guest checkout",
                        "expected_invariant": "Checkout is submitted",
                    }
                ],
            },
        )
        assert result.structured_content["status"] == "ok"
        assert "APPROVED_COVERAGE_MAP" in drafting.prompts[0]
        assert "Coverage ID: COV-GUEST-HAPPY" in drafting.prompts[0]
        assert "Expected invariant: Checkout is submitted" in drafting.prompts[0]
        assert result.structured_content["quality_status"] == "canary"

        feedback = await client.call_tool(
            "record_canary_feedback",
            {
                "draft_id": result.structured_content["draft_id"],
                "verdict": "edited",
                "reason": "coverage",
            },
        )
        assert feedback.structured_content == {
            "status": "recorded",
            "feedback_count": 1,
            "target": CANARY_TARGET,
        }

        task_outcome = await client.call_tool(
            "record_qa_task_outcome",
            {
                "task_type": "ordinary_review",
                "outcome": "completed",
                "codegraph_calls": 1,
                "source_mcp_calls": 4,
                "qwen_used": True,
                "sol_used": False,
                "findings_identified": 2,
                "findings_confirmed": 1,
                "findings_rejected": 1,
                "qwen_edits": 1,
                "repeated_source_reads": 0,
            },
        )
        assert task_outcome.structured_content == {"status": "recorded"}

        report = await client.call_tool("get_metrics_report", {"days": 7})
        assert report.structured_content["qa_tasks"]["events"] == 1
        assert report.structured_content["qa_tasks"]["qwen_tasks"] == 1


@pytest.mark.asyncio
async def test_review_checklist_tool_uses_selected_profile(tmp_path):
    drafting = DraftFake()
    service = RouterService(Settings(data_dir=tmp_path), drafting)

    async with Client(build_server(service)) as client:
        result = await client.call_tool(
            "draft_review_checklist",
            {
                "agent_profile": "security_reviewer",
                "evidence_packet": "Changed login validation; runtime evidence is unverified.",
                "project_pattern": "Use existing WebdriverIO/Appium test conventions.",
            },
        )

    assert result.structured_content["status"] == "ok"
    assert "REVIEW_AGENT_PROFILE: security_reviewer" in drafting.prompts[0]
    assert "WebdriverIO/Appium" in drafting.prompts[0]


@pytest.mark.asyncio
async def test_metrics_report_rejects_non_positive_days(tmp_path):
    service = RouterService(Settings(data_dir=tmp_path), DraftFake())

    async with Client(build_server(service)) as client:
        with pytest.raises(ToolError, match="days must be positive"):
            await client.call_tool("get_metrics_report", {"days": 0})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments", "prompt_markers"),
    [
        (
            "translate_text",
            {
                "text": "Check guest checkout",
                "target_language": "Russian",
                "preserve_terms": "checkout",
            },
            ("Check guest checkout", "Russian", "checkout"),
        ),
        (
            "rewrite_text",
            {"text": "The test has failed", "instruction": "Make it concise"},
            ("The test has failed", "Make it concise"),
        ),
        (
            "explain_short",
            {"topic": "What is idempotency?", "audience": "QA engineer"},
            ("What is idempotency?", "QA engineer", "120 words"),
        ),
        (
            "summarize_text",
            {"text": "First fact. Second fact.", "focus": "Risks"},
            ("First fact. Second fact.", "Risks"),
        ),
    ],
)
async def test_text_tools_pass_bounded_instructions_to_local_backend(
    tmp_path,
    tool_name,
    arguments,
    prompt_markers,
):
    drafting = DraftFake()
    service = RouterService(Settings(data_dir=tmp_path), drafting)

    async with Client(build_server(service)) as client:
        result = await client.call_tool(tool_name, arguments)

    assert result.structured_content["status"] == "ok"
    assert all(marker in drafting.prompts[0] for marker in prompt_markers)


@pytest.mark.asyncio
async def test_test_case_tool_rejects_duplicate_coverage_ids(tmp_path):
    drafting = DraftFake()
    service = RouterService(Settings(data_dir=tmp_path), drafting)
    item = {
        "coverage_id": "COV-DUPLICATE",
        "purpose": "Branch",
        "source": "Confirmed requirement",
        "state": "Ready",
        "expected_invariant": "Stable result",
    }

    async with Client(build_server(service)) as client:
        with pytest.raises(ToolError, match="coverage IDs must be unique"):
            await client.call_tool(
                "draft_test_cases",
                {"requirement": "Requirement", "coverage_map": [item, item]},
            )
