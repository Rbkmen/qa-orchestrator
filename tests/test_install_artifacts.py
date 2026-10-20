import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from qa_orchestrator.mcp_launcher import sanitized_environment

ROOT = Path(__file__).parents[1]
LAUNCHER = ROOT / "scripts/qa-orchestrator"
CLIENT_RULE_FILES = tuple(
    sorted(path for pattern in ("*.md", "*.mdc") for path in (ROOT / "client-rules").rglob(pattern))
)
USER_CONTROLLED_SPEED_PATTERNS = (
    r"execution speed and latency preferences (?:are|remain) controlled by the user's host/provider settings",
    r"execution speed and latency follow the user's host/provider settings",
    r"execution speed and latency preferences are controlled by the host user's settings",
)
CLIENT_RULE_CONTRACT = {
    "orchestration entry": (r"\bstart_qa_orchestration\b",),
    "route checklist source": (r"\b(?:prepare_qa_orchestration|selected_review_routes)\b",),
    "state read": (r"\bget_qa_orchestration\b",),
    "orchestration finalization": (r"\bfinish_qa_orchestration\b",),
    "single route selection": (r"exactly one.*(?:bundle|profile)",),
    "model-neutral stage labels": (r"stage labels and status text model-neutral",),
    "user-controlled execution speed": USER_CONTROLLED_SPEED_PATTERNS,
    "risk signals": (r"\brisk_signals\b",),
    "completed profile": (r"\bcompleted_profile\b",),
    "execution group schedule": (r"\bexecution_groups\b",),
    "parallel profile launch": (r"same group.{0,120}concurrently",),
    "sequential fallback": (r"sequential fallback",),
    "ordered profile reporting": (r"report each profile.{0,120}fixed (?:route )?order",),
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
    "MCP failure fallback": (r"MCP is unavailable",),
    "terminal task outcome": (r"final (?:reported )?(?:status|outcome)",),
    "run-id consistency": (r"run_id",),
    "host-default model mode": (r"\bhost_default\b",),
    "reported sequence limit": (
        r"orchestrator validates.*host reports.*cannot confirm",
    ),
    "risk-signal evidence limit": (
        r"cannot verify.*risk_signals.*reflect evidence",
    ),
}


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell launcher")
def test_launcher_is_executable_valid_shell():
    result = subprocess.run(
        ["/bin/sh", "-n", str(LAUNCHER)],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert os.access(LAUNCHER, os.X_OK)


def test_launcher_forwards_supported_overrides():
    content = LAUNCHER.read_text(encoding="utf-8")

    assert "qa-orchestrator-mcp" in content
    assert (
        'QA_ORCHESTRATOR_ORCHESTRATION_TTL_SECONDS="${QA_ORCHESTRATOR_ORCHESTRATION_TTL_SECONDS:-1800}"'
        in content
    )
    assert (
        'QA_ORCHESTRATOR_ORCHESTRATION_MAX_SESSIONS="${QA_ORCHESTRATOR_ORCHESTRATION_MAX_SESSIONS:-100}"'
        in content
    )
    assert 'QA_ORCHESTRATOR_MODEL_POLICY_PATH="$orchestrator_model_policy_path"' in content
    assert 'QA_ORCHESTRATOR_SESSION_STORE_PATH="$orchestrator_session_store_path"' in content


def test_mcp_launcher_keeps_only_supported_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("QA_ORCHESTRATOR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("QA_ORCHESTRATOR_ORCHESTRATION_TTL_SECONDS", "900")
    monkeypatch.setenv("PRIVATE_ACCESS_TOKEN", "must-not-reach-server")

    environment = sanitized_environment()

    assert environment["HOME"] == str(tmp_path)
    assert environment["QA_ORCHESTRATOR_DATA_DIR"] == str(tmp_path / "data")
    assert environment["QA_ORCHESTRATOR_ORCHESTRATION_TTL_SECONDS"] == "900"
    assert "PRIVATE_ACCESS_TOKEN" not in environment
    assert "OPENAI_API_KEY" not in environment


def test_client_rule_templates_preserve_orchestration_contract():
    assert CLIENT_RULE_FILES
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
        assert "finish_qa_orchestration" in text, artifact.relative_to(ROOT)


def test_ci_uses_immutable_actions_and_builds_wheel():
    content = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    refs = re.findall(r"uses:\s+\S+@([^\s#]+)", content)

    assert refs
    assert all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in refs)
    assert 'version: "0.11.30"' in content
    assert "uv audit" not in content
    assert "schedule:" not in content
    assert "workflow_dispatch:" not in content
    assert "concurrency:" in content
    assert "cancel-in-progress: true" in content
    assert "github.event.pull_request.number || github.ref" in content
    assert "uv build --wheel --out-dir dist" in content
    assert 'os: ["ubuntu-latest", "windows-latest"]' in content
    assert '"uv", "pip", "install"' in content
    assert "working-directory: ${{ runner.temp }}" in content
    assert (
        'run: uv run --project "${{ github.workspace }}" --no-sync python -m qa_orchestrator.doctor --json'
        in content
    )


def test_dependency_audit_is_separate_and_keeps_manual_weekly_triggers():
    content = (ROOT / ".github/workflows/dependency-audit.yml").read_text(encoding="utf-8")
    refs = re.findall(r"uses:\s+\S+@([^\s#]+)", content)

    assert refs
    assert all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in refs)
    assert '"pyproject.toml"' in content
    assert '"uv.lock"' in content
    assert "pull_request:\n    paths:" in content
    assert "push:\n    branches:\n      - main\n    paths:" in content
    assert 'cron: "17 8 * * 1"' in content
    assert "workflow_dispatch:" in content
    assert "cancel-in-progress: true" in content
    assert 'version: "0.11.30"' in content
    assert "UV_PREVIEW_FEATURES: audit-command" in content
    assert content.index("uv lock --check") < content.index("uv audit --locked")
    assert "run: uv audit --locked" in content
    assert "pytest" not in content


def test_package_exposes_public_metadata_and_doctor():
    content = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert 'readme = "README.md"' in content
    assert 'qa-orchestrator-doctor = "qa_orchestrator.doctor:main"' in content
    assert 'qa-orchestrator-mcp = "qa_orchestrator.mcp_launcher:main"' in content
    assert "[project.urls]" in content
    assert 'Homepage = "https://github.com/Rbkmen/qa-orchestrator"' in content


def test_operational_artifacts_describe_primary_agent_routing():
    artifacts = [
        ROOT / "README.md",
        ROOT / "docs/ORCHESTRATION_POLICY.md",
        ROOT / "client-rules/generic/QA_ORCHESTRATOR_INSTRUCTIONS.md",
    ]

    for artifact in artifacts:
        text = artifact.read_text(encoding="utf-8").lower()
        assert "prepare_qa_orchestration" in text
        for forbidden in ("q" + "wen", "lm " + "studio", "local " + "delegation"):
            assert forbidden not in text


def test_operational_artifacts_describe_host_orchestration():
    artifacts = [
        ROOT / "README.md",
        ROOT / "docs/ORCHESTRATION_POLICY.md",
        ROOT / "client-rules/generic/QA_ORCHESTRATOR_INSTRUCTIONS.md",
        ROOT / "docs/clients/codex.md",
        ROOT / "docs/clients/claude-code.md",
    ]
    required_policy = ("model_policy",)
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
        assert any(re.search(pattern, text) for pattern in USER_CONTROLLED_SPEED_PATTERNS), artifact
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


def test_canonical_client_rules_use_transition_state_and_recovery_read():
    rules = (ROOT / "client-rules/generic/QA_ORCHESTRATOR_INSTRUCTIONS.md").read_text(
        encoding="utf-8"
    )

    assert "updated session returned by `advance_qa_orchestration`" in rules
    assert "only to resume or recover" in rules
    assert "use `get_qa_orchestration` to determine the next action" not in rules


def test_operational_artifacts_describe_review_bundles_and_statuses():
    artifacts = [
        ROOT / "docs/ORCHESTRATION_POLICY.md",
        ROOT / "client-rules/generic/QA_ORCHESTRATOR_INSTRUCTIONS.md",
    ]
    required = (
        "ordinary_mr",
        "widget",
        "widget_js",
        "ruby_backend",
        "security",
        "autotest",
        "requirements",
        "faraday — evidence investigator",
        "triage",
        "primary review",
        "deep review",
        "final synthesis",
        "final qa outcome",
    )
    forbidden = (
        "faraday service",
        "faraday provider",
        "qa orchestrator calls models",
        "submit evidence to qa orchestrator",
        "luna / max",
        "sol / medium",
        "sol / high",
    )

    for artifact in artifacts:
        text = artifact.read_text(encoding="utf-8").lower()
        for phrase in required:
            assert phrase in text, f"{phrase} is missing from {artifact}"
        for phrase in forbidden:
            assert phrase not in text


def test_uvx_update_keeps_existing_model_policy():
    installation = (ROOT / "docs/INSTALLATION.md").read_text(encoding="utf-8")
    update = installation.split("For a no-checkout installation", 1)[1].split(
        "For Claude Code", 1
    )[0]

    assert "@<commit-sha>' qa-orch setup" not in update
    assert "Do not rerun `qa-orch setup` as part of a" in update
    assert "only if you want to configure explicit per-stage models" in update


def test_policy_docs_tie_deep_review_to_risk_assessment():
    policy = (ROOT / "docs/ORCHESTRATION_POLICY.md").read_text(encoding="utf-8")

    assert "according to the deep-review assessment" in policy
    assert "according to the active model-policy mode" not in policy


def test_review_instructions_reuse_evidence_and_escalate_only_actionable_gaps():
    instructions = (
        ROOT / "client-rules/generic/QA_ORCHESTRATOR_INSTRUCTIONS.md"
    ).read_text(encoding="utf-8")
    policy = (ROOT / "docs/ORCHESTRATION_POLICY.md").read_text(encoding="utf-8")

    assert "Inspect the full diff once" in instructions
    assert "reopen only the source needed" in instructions
    assert "CI, Stage, or runtime checks were not run" in policy
    assert "carry the named unresolved question" in policy


def test_readme_documents_pinned_installation_and_review_layers():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    quick_start = readme.split("## Quick start", 1)[1].split("\n## ", 1)[0]

    assert "codex mcp add qa-orchestrator -- uvx" in quick_start
    assert re.search(
        r"git\+https://github\.com/Rbkmen/qa-orchestrator\.git@<commit-sha>",
        quick_start,
    )
    assert "qa-orchestrator-mcp" in quick_start
    assert "qa-orch setup" not in quick_start
    assert "Ruby" in readme
    assert "ordinary qa" in readme.lower()
    assert "advance_qa_orchestration" in readme
    assert "[bundle and profile catalog](docs/ORCHESTRATION_POLICY.md#fixed-bundles-and-profile-names)" in readme
    assert "[complete MCP tool contract](docs/ORCHESTRATION_POLICY.md#mcp-tools)" in readme
    for heading in (
        "Architecture and responsibilities", "Review workflow", "Profiles and bundles",
        "Evidence and context", "Findings and final decisions", "Host execution",
        "Performance and evaluation", "MCP tools", "CLI tools", "Configuration",
        "Storage and trust boundaries",
    ):
        assert f"## {heading}" in readme

    from qa_orchestrator.contracts import ReviewAgent, ReviewBundle

    for identifier in (*ReviewAgent, *ReviewBundle):
        assert f"`{identifier.value}`" in readme


def test_host_default_is_documented_as_the_no_setup_first_run():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    installation = (ROOT / "docs/INSTALLATION.md").read_text(encoding="utf-8")
    policy = (ROOT / "docs/ORCHESTRATION_POLICY.md").read_text(encoding="utf-8")

    assert "host_default" in readme
    assert "setup --host-default" in installation
    assert "host_default" in policy
    assert "cannot confirm" in policy.lower()


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
async def test_launcher_exposes_ten_tools(monkeypatch, tmp_path):
    virtual_env = str(Path(sys.executable).parent.parent)
    monkeypatch.setenv("VIRTUAL_ENV", virtual_env)
    monkeypatch.setenv("QA_ORCHESTRATOR_DATA_DIR", str(tmp_path))
    command = (
        str(Path(sys.executable).with_name("qa-orchestrator-mcp.exe"))
        if os.name == "nt"
        else str(LAUNCHER)
    )
    transport = StdioTransport(
        command=command,
        args=[],
        env={
            **os.environ,
            "VIRTUAL_ENV": virtual_env,
            "QA_ORCHESTRATOR_DATA_DIR": str(tmp_path),
            "QA_ORCHESTRATOR_SESSION_STORE_PATH": "",
        },
    )

    try:
        async with Client(transport) as client:
            names = {tool.name for tool in await client.list_tools()}
            prepared = await client.call_tool(
                "prepare_qa_orchestration",
                {"agent_profile": "code_explorer"},
            )
            prepared_batch = await client.call_tool(
                "prepare_qa_orchestration_bundle",
                {"bundle": "ruby_backend"},
            )
            listed = await client.call_tool("list_qa_orchestrations", {})
            catalog = await client.call_tool("get_qa_orchestration_catalog", {})
            started = await client.call_tool("start_qa_orchestration", {"task_type": "other"})
            deleted = await client.call_tool(
                "delete_qa_orchestration", {"run_id": started.structured_content["run_id"]}
            )
            after_delete = await client.call_tool("list_qa_orchestrations", {})
    finally:
        await transport.close()

    assert names == {
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
    assert prepared.structured_content["profile"] == "code_explorer"
    assert prepared_batch.structured_content["bundle"] == "ruby_backend"
    assert [route["profile"] for route in prepared_batch.structured_content["routes"]] == [
        "code_explorer",
        "ruby_reviewer",
        "pr_test_analyzer",
        "omission_auditor",
    ]
    assert deleted.structured_content == {
        "run_id": started.structured_content["run_id"], "deleted": True
    }
    assert after_delete.structured_content["sessions"] == []
    assert listed.structured_content == {
        "sessions": [], "read_only": True, "host_owns_decisions": True
    }
    assert any(
        entry["bundle"] == "python_backend" for entry in catalog.structured_content["bundles"]
    )


@pytest.mark.asyncio
async def test_server_startup_makes_no_update_check_network_requests(tmp_path):
    marker = tmp_path / "network-attempt"
    dependency_home = tmp_path / "fastmcp"
    script = """
import ipaddress
import os
import sys
from pathlib import Path
import fastmcp

fastmcp.settings.home = Path(os.environ['CHECK_DEPENDENCY_HOME'])
fastmcp.settings.check_for_updates = 'stable'

def is_loopback(host):
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode('ascii', errors='ignore')
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except (TypeError, ValueError):
        return False

def forbid_network(event, args):
    if event == 'socket.getaddrinfo' and not is_loopback(args[0]):
        Path(os.environ['CHECK_NETWORK_MARKER']).touch()
        raise PermissionError('External network is unavailable in this local startup check')
    if event == 'socket.connect':
        address = args[1]
        if not isinstance(address, tuple) or not is_loopback(address[0]):
            Path(os.environ['CHECK_NETWORK_MARKER']).touch()
            raise PermissionError('External network is unavailable in this local startup check')

sys.addaudithook(forbid_network)
from qa_orchestrator.server import main
main()
"""
    transport = StdioTransport(
        command=sys.executable,
        args=["-c", script],
        env={
            **os.environ,
            "CHECK_DEPENDENCY_HOME": str(dependency_home),
            "CHECK_NETWORK_MARKER": str(marker),
            "FASTMCP_SHOW_SERVER_BANNER": "true",
        },
    )
    try:
        async with Client(transport) as client:
            result = await client.call_tool("get_qa_orchestration_model_policy", {})
    finally:
        await transport.close()

    assert result.structured_content == {"mode": "host_default"}
    assert not marker.exists(), "Server startup attempted an outbound request"
    assert not dependency_home.exists(), "Server startup created a dependency update cache"
