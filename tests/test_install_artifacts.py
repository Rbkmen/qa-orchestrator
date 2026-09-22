import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

ROOT = Path(__file__).parents[1]
LAUNCHER = ROOT / "scripts/qa-orchestrator"
CLIENT_RULE_FILES = tuple(
    sorted(
        path
        for pattern in ("*.md", "*.mdc")
        for path in (ROOT / "client-rules").rglob(pattern)
    )
)
CLIENT_RULE_CONTRACT = {
    "orchestration entry": (r"\bstart_qa_orchestration\b",),
    "route preparation": (r"\bprepare_review_route\b",),
    "state read": (r"\bget_qa_orchestration\b",),
    "content-free metrics": (r"\brecord_qa_task_outcome\b",),
    "single route selection": (r"exactly one.*(?:bundle|profile)",),
    "Luna model": (r"gpt-6-luna",),
    "Luna reasoning": (r"luna/max",),
    "Sol model": (r"gpt-6-sol",),
    "Sol primary reasoning": (r"sol/medium",),
    "Sol deep reasoning": (r"sol/high",),
    "fixed speed": (r"speed=1\.0",),
    "risk signals": (r"\brisk_signals\b",),
    "completed profile": (r"\bcompleted_profile\b",),
    "read-only boundary": (r"read_only=true",),
    "host-owned decisions": (r"host_owns_decisions=true",),
    "host-owned evidence": (
        r"host owns evidence",
        r"owns evidence.*(?:decisions|external writes)",
        r"owner of evidence",
    ),
    "no raw orchestration input": (
        r"do not pass evidence, prompts, or model outputs",
        r"pass no raw evidence.*model output",
        r"does not accept evidence, prompts, or model outputs",
    ),
}


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

    assert 'QA_ORCHESTRATOR_METRICS_RETENTION_DAYS="${QA_ORCHESTRATOR_METRICS_RETENTION_DAYS:-30}"' in content
    assert 'QA_ORCHESTRATOR_METRICS_MAX_EVENTS="${QA_ORCHESTRATOR_METRICS_MAX_EVENTS:-10000}"' in content
    assert 'QA_ORCHESTRATOR_ORCHESTRATION_TTL_SECONDS="${QA_ORCHESTRATOR_ORCHESTRATION_TTL_SECONDS:-1800}"' in content
    assert 'QA_ORCHESTRATOR_ORCHESTRATION_MAX_SESSIONS="${QA_ORCHESTRATOR_ORCHESTRATION_MAX_SESSIONS:-100}"' in content


def test_client_rule_templates_preserve_orchestration_contract():
    assert CLIENT_RULE_FILES
    v2_metric_fields = (
        "triage_calls",
        "primary_review_calls",
        "deep_review_calls",
        "synthesis_calls",
        "triage_input_tokens",
        "triage_output_tokens",
        "primary_review_input_tokens",
        "primary_review_output_tokens",
        "synthesis_input_tokens",
        "synthesis_output_tokens",
    )
    legacy_model_fields = (
        "luna_calls",
        "terra_calls",
        "sol_calls",
        "luna_input_tokens",
        "luna_output_tokens",
        "terra_primary_input_tokens",
        "terra_primary_output_tokens",
        "terra_synthesis_input_tokens",
        "terra_synthesis_output_tokens",
    )

    for artifact in CLIENT_RULE_FILES:
        text = artifact.read_text(encoding="utf-8")
        missing = [
            label
            for label, alternatives in CLIENT_RULE_CONTRACT.items()
            if not any(
                re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
                for pattern in alternatives
            )
        ]

        assert not missing, f"{artifact.relative_to(ROOT)} is missing: {', '.join(missing)}"
        assert all(field in text for field in v2_metric_fields), artifact.relative_to(ROOT)
        assert not any(field in text for field in legacy_model_fields), artifact.relative_to(ROOT)

    generic_rules = (ROOT / "client-rules/generic/QA_ORCHESTRATOR_INSTRUCTIONS.md").read_text(
        encoding="utf-8"
    )
    assert "v1" in generic_rules.lower()


def test_ci_uses_immutable_action_refs_and_builds_wheel():
    content = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    refs = re.findall(r"uses:\s+\S+@([^\s#]+)", content)

    assert refs
    assert all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in refs)
    assert "uv build --wheel --out-dir dist" in content


def test_operational_artifacts_describe_primary_agent_routing():
    artifacts = [
        ROOT / "README.md",
        ROOT / "docs/ORCHESTRATION_POLICY.md",
        ROOT / "client-rules/generic/QA_ORCHESTRATOR_INSTRUCTIONS.md",
    ]

    for artifact in artifacts:
        text = artifact.read_text(encoding="utf-8").lower()
        assert "prepare_review_route" in text
        for forbidden in ("q" + "wen", "lm " + "studio", "local " + "delegation"):
            assert forbidden not in text


def test_operational_artifacts_describe_host_orchestration():
    artifacts = [
        ROOT / "README.md",
        ROOT / "docs/ORCHESTRATION_POLICY.md",
        ROOT / "client-rules/generic/QA_ORCHESTRATOR_INSTRUCTIONS.md",
        ROOT / "docs/clients/codex.md",
        ROOT / "docs/clients/claude-code.md",
        ROOT / "docs/clients/cursor.md",
        ROOT / "docs/clients/generic-mcp.md",
    ]
    required_policy = (
        "gpt-6-luna",
        "gpt-6-sol",
        "speed=1.0",
    )
    host_contract_artifacts = {
        ROOT / "README.md",
        ROOT / "docs/ORCHESTRATION_POLICY.md",
        ROOT / "client-rules/generic/QA_ORCHESTRATOR_INSTRUCTIONS.md",
    }
    forbidden = (
        "qa orchestrator calls models",
        "qa orchestrator invokes models",
        "submit evidence to qa orchestrator",
        "arbitrary prompt to qa orchestrator",
    )

    for artifact in artifacts:
        text = artifact.read_text(encoding="utf-8").lower()
        for phrase in required_policy:
            assert phrase in text
        if artifact in host_contract_artifacts:
            for tool in (
                "start_qa_orchestration",
                "advance_qa_orchestration",
                "get_qa_orchestration",
            ):
                assert tool in text
            assert "host_owns_decisions" in text
        for phrase in forbidden:
            assert phrase not in text


def test_operational_artifacts_describe_review_bundles_and_statuses():
    artifacts = [
        ROOT / "README.md",
        ROOT / "docs/ORCHESTRATION_POLICY.md",
        ROOT / "client-rules/generic/QA_ORCHESTRATOR_INSTRUCTIONS.md",
    ]
    required = (
        "ordinary_mr",
        "widget",
        "security",
        "autotest",
        "requirements",
        "faraday — evidence investigator",
        "luna / max",
        "sol / medium",
        "sol / high",
        "host → final qa outcome",
    )
    forbidden = (
        "faraday service",
        "faraday provider",
        "qa orchestrator calls models",
        "submit evidence to qa orchestrator",
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
    forbidden = ("local model", "local delegation")

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
