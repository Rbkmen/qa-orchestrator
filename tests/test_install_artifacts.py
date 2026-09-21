import os
import re
import subprocess
import sys
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


def test_ci_uses_immutable_action_refs_and_builds_wheel():
    content = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    refs = re.findall(r"uses:\s+\S+@([^\s#]+)", content)

    assert refs
    assert all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in refs)
    assert "uv build --wheel --out-dir dist" in content


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


def test_operational_artifacts_describe_host_orchestration():
    artifacts = [
        ROOT / "README.md",
        ROOT / "docs/ROUTING_POLICY.md",
        ROOT / "client-rules/generic/QA_ROUTER_INSTRUCTIONS.md",
    ]
    required = (
        "start_qa_orchestration",
        "advance_qa_orchestration",
        "get_qa_orchestration",
        "gpt-5.6-luna",
        "gpt-5.6-terra",
        "gpt-5.6-sol",
        "speed=1.0",
        "host_owns_decisions",
    )
    forbidden = (
        "qa router calls models",
        "qa router invokes models",
        "submit evidence to qa router",
        "arbitrary prompt to qa router",
    )

    for artifact in artifacts:
        text = artifact.read_text(encoding="utf-8").lower()
        for phrase in required:
            assert phrase in text
        for phrase in forbidden:
            assert phrase not in text


def test_operational_artifacts_describe_review_bundles_and_statuses():
    artifacts = [
        ROOT / "README.md",
        ROOT / "docs/ROUTING_POLICY.md",
        ROOT / "client-rules/generic/QA_ROUTER_INSTRUCTIONS.md",
    ]
    required = (
        "ordinary_mr",
        "widget",
        "security",
        "autotest",
        "requirements",
        "faraday — evidence investigator",
        "luna / max",
        "terra / medium",
        "sol / high",
        "host → final qa outcome",
    )
    forbidden = (
        "faraday service",
        "faraday provider",
        "qa router calls models",
        "submit evidence to qa router",
    )

    for artifact in artifacts:
        text = artifact.read_text(encoding="utf-8").lower()
        for phrase in required:
            assert phrase in text, f"{phrase} is missing from {artifact}"
        for phrase in forbidden:
            assert phrase not in text


def test_documentation_contains_no_retired_runtime_terms():
    artifacts = [
        ROOT / "README.md",
        ROOT / "CONTRIBUTING.md",
        *ROOT.glob("docs/**/*.md"),
        *ROOT.glob("client-rules/**/*.md"),
        *ROOT.glob("client-rules/**/*.mdc"),
    ]
    forbidden = ("qwen", "lmstudio", "lm studio", "llmster", "mlx", "local model", "local delegation")

    for artifact in artifacts:
        text = artifact.read_text(encoding="utf-8").lower()
        for term in forbidden:
            assert term not in text, f"{term} remains in {artifact}"


@pytest.mark.asyncio
async def test_launcher_exposes_six_tools(monkeypatch):
    virtual_env = str(Path(sys.executable).parent.parent)
    monkeypatch.setenv("VIRTUAL_ENV", virtual_env)
    transport = StdioTransport(
        command=str(LAUNCHER),
        args=[],
        env={**os.environ, "VIRTUAL_ENV": virtual_env},
    )

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
