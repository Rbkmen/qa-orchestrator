# Cursor setup

Cursor connects QA Orchestrator through `mcp.json`. Use `$HOME/.cursor/mcp.json` for all projects or `.cursor/mcp.json` for one project.

## Connection

Create or merge the configuration without overwriting existing servers:

```json
{
  "mcpServers": {
    "qa-orchestrator": {
      "command": "/absolute/path/to/qa-orchestrator/scripts/qa-orchestrator",
      "args": []
    }
  }
}
```

Restart Cursor and verify that exactly six QA Orchestrator tools are available.

## Host-agent instructions

```bash
mkdir -p /absolute/path/to/your-project/.cursor/rules
cp /absolute/path/to/qa-orchestrator/client-rules/cursor/qa-orchestrator.mdc \
  /absolute/path/to/your-project/.cursor/rules/qa-orchestrator.mdc
```

The rule makes Cursor Agent obtain evidence, run model stages, and make QA decisions independently. Use `start_qa_orchestration` → Luna selects a fixed bundle or profile → `advance_qa_orchestration` → `get_qa_orchestration`; use `prepare_review_route` for every role in the fixed order, not as a separate external agent. For `ordinary_mr`, the order is `Faraday — Evidence Investigator` → `Code Reviewer` → `Test Analyzer`; after every role, pass its `completed_profile`, and on the last role pass `risk_signals` in the same call so the orchestrator selects Sol or synthesis. Do not pass `risk_signals` on the later synthesis transition. Policy: Luna/max for triage, Terra/medium for primary review and synthesis, and optional Sol/high for read-only escalation, always with `speed=1.0`. The final `record_qa_task_outcome` receives the original `run_id`.

References: [official Cursor MCP documentation](https://docs.cursor.com/context/model-context-protocol) and [Cursor Rules documentation](https://cursor.com/docs/rules).
