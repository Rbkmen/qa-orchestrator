# Codex setup

Codex connects QA Orchestrator as a local STDIO MCP server. The launcher uses the project's `.venv`, the active `VIRTUAL_ENV`, or an installed `qa-orchestrator` from `PATH`.

## Connection

From the repository directory, get the absolute path:

```bash
pwd
```

Add the server:

```bash
codex mcp add qa-orchestrator -- \
  /absolute/path/to/qa-orchestrator/scripts/qa-orchestrator
```

Or add it to `$HOME/.codex/config.toml`:

```toml
[mcp_servers.qa-orchestrator]
command = "/absolute/path/to/qa-orchestrator/scripts/qa-orchestrator"
args = []
startup_timeout_sec = 30
tool_timeout_sec = 120
```

Verify the registration with `codex mcp list` and restart Codex.

## Host-agent instructions

Add the rules from [`client-rules/generic/QA_ORCHESTRATOR_INSTRUCTIONS.md`](../../client-rules/generic/QA_ORCHESTRATOR_INSTRUCTIONS.md) to persistent project instructions, or adapt them to your Codex rules. Do not install a separate routing skill: the MCP server and these instructions are sufficient.

The linked generic file is canonical for orchestration mechanics. Workspace and repository rules should add only task-specific routing, safety, and output requirements.

Primary Codex remains the host and owner of evidence, decisions, and external actions. Use the six tools for orchestration: `start_qa_orchestration`, `advance_qa_orchestration`, `get_qa_orchestration`, `prepare_review_route`, `record_qa_task_outcome`, and `get_metrics_report`.

The model policy is host-owned: `gpt-5.6-luna/max` performs triage, `gpt-5.6-terra/medium` performs primary review and synthesis, and optional `gpt-5.6-sol/high` performs read-only deep analysis. Keep `speed=1.0` for every stage. Send the orchestrator only structured signals; prompts, evidence, and model outputs remain in Codex.

During Luna triage, select exactly one fixed bundle or one compatibility profile, never both. For the ordinary MR bundle, the order is `code_explorer` (`Faraday — Evidence Investigator`) → `code_reviewer` (`Code Reviewer`) → `pr_test_analyzer` (`Test Analyzer`). After every Terra role, pass its identifier as `completed_profile`; on the last role, pass `risk_signals` in the same call so the orchestrator can select Sol or synthesis. Do not pass `risk_signals` on the later synthesis transition. Faraday is an internal profile name, not a separate external service or model. Show statuses as `Luna / Max` → `Terra / Medium` per role → optional `Sol / High` → `Terra / Medium` synthesis → `Host` final outcome; pass the original `run_id` to the final `record_qa_task_outcome`.

For a low-risk, one-repository change with one narrow concern, prefer one compatibility profile: `code_reviewer` for behavior/callers, `pr_test_analyzer` for test-only changes, `typescript_reviewer` for TypeScript-only changes, or `react_reviewer` for React-only changes. Keep one compact Evidence Packet with `E1`-style references and bounded `F-01` finding candidates; pass only the relevant sections to each role.

Reference: [official Codex MCP documentation](https://developers.openai.com/codex/mcp).
