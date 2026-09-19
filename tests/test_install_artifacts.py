import os
import subprocess
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

ROOT = Path(__file__).parents[1]
LAUNCHER = ROOT / "scripts/qa-router-mcp"


def test_launcher_is_executable_valid_shell():
    result = subprocess.run(
        ["/bin/sh", "-n", str(LAUNCHER)],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert os.access(LAUNCHER, os.X_OK)


def test_launcher_forwards_metrics_overrides():
    content = LAUNCHER.read_text(encoding="utf-8")

    assert 'QA_ROUTER_METRICS_RETENTION_DAYS="${QA_ROUTER_METRICS_RETENTION_DAYS:-30}"' in content
    assert 'QA_ROUTER_METRICS_MAX_EVENTS="${QA_ROUTER_METRICS_MAX_EVENTS:-10000}"' in content
    assert 'QA_ROUTER_ORCHESTRATION_TTL_SECONDS="${QA_ROUTER_ORCHESTRATION_TTL_SECONDS:-1800}"' in content
    assert 'QA_ROUTER_ORCHESTRATION_MAX_SESSIONS="${QA_ROUTER_ORCHESTRATION_MAX_SESSIONS:-100}"' in content


def test_operational_artifacts_describe_primary_agent_routing():
    artifacts = [
        ROOT / "README.md",
        ROOT / "docs/ROUTING_POLICY.md",
        ROOT / "client-rules/generic/QA_ROUTER_INSTRUCTIONS.md",
    ]

    for artifact in artifacts:
        text = artifact.read_text(encoding="utf-8").lower()
        assert "prepare_review_route" in text
        for forbidden in ("q" + "wen", "lm " + "studio", "local " + "delegation"):
            assert forbidden not in text


@pytest.mark.asyncio
async def test_launcher_exposes_six_tools():
    transport = StdioTransport(command=str(LAUNCHER), args=[])

    try:
        async with Client(transport) as client:
            names = {tool.name for tool in await client.list_tools()}
    finally:
        await transport.close()

    assert names == {
        "prepare_review_route",
        "record_qa_task_outcome",
        "get_metrics_report",
        "start_qa_orchestration",
        "advance_qa_orchestration",
        "get_qa_orchestration",
    }
