# Codex setup

Codex connects QA Router as a local STDIO MCP server. The launcher uses the project's `.venv`, the active `VIRTUAL_ENV`, or an installed `qa-router-mcp` from `PATH`.

## Connection

From the repository directory, get the absolute path:

```bash
pwd
```

Add the server:

```bash
codex mcp add qa-router -- \
  /absolute/path/to/qa-router-mcp/scripts/qa-router-mcp
```

Or add it to `$HOME/.codex/config.toml`:

```toml
[mcp_servers.qa-router]
command = "/absolute/path/to/qa-router-mcp/scripts/qa-router-mcp"
args = []
startup_timeout_sec = 30
tool_timeout_sec = 120
```

Verify the registration with `codex mcp list` and restart Codex.

## Host-agent instructions

Add the rules from [`client-rules/generic/QA_ROUTER_INSTRUCTIONS.md`](../../client-rules/generic/QA_ROUTER_INSTRUCTIONS.md) to persistent project instructions, or adapt them to your Codex rules. Do not install a separate routing skill: the MCP server and these instructions are sufficient.

Primary Codex remains the host and owner of evidence, decisions, and external actions. Use the six tools for orchestration: `start_qa_orchestration`, `advance_qa_orchestration`, `get_qa_orchestration`, `prepare_review_route`, `record_qa_task_outcome`, and `get_metrics_report`.

The model policy is host-owned: `gpt-5.6-luna/max` performs triage, `gpt-5.6-terra/medium` performs primary review and synthesis, and optional `gpt-5.6-sol/high` performs read-only deep analysis. Send the router only structured signals; prompts, evidence, and model outputs remain in Codex.

During Luna triage, select a fixed bundle or one compatibility profile. For the ordinary MR bundle, the order is `code_explorer` (`Faraday — Evidence Investigator`) → `code_reviewer` (`Code Reviewer`) → `pr_test_analyzer` (`Test Analyzer`). After every Terra role, pass its identifier as `completed_profile`; synthesis or Sol is available only after the last role. Faraday is an internal profile name, not a separate external service or model. Show statuses as `Luna / Max` → `Terra / Medium` per role → `Terra / Medium` synthesis → `Host` final outcome; pass the original `run_id` to the final `record_qa_task_outcome`.

Reference: [official Codex MCP documentation](https://developers.openai.com/codex/mcp).
