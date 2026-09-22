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

The rule makes Cursor Agent obtain evidence, run model stages, and make QA decisions independently. Use `start_qa_orchestration` → Luna selects a fixed bundle or profile → `advance_qa_orchestration` → `get_qa_orchestration`; use `prepare_review_route` for every role in the fixed order, not as a separate external agent. For `ordinary_mr`, the order is `Faraday — Evidence Investigator` → `Code Reviewer` → `Test Analyzer`; after every role, pass its `completed_profile`, and on the last role pass `risk_signals` in the same call so the orchestrator selects Sol deep review or synthesis. Do not pass `risk_signals` on the later synthesis transition. Policy: `gpt-6-luna/max` for triage, `gpt-6-sol/medium` for primary review and synthesis, and optional `gpt-6-sol/high` for read-only escalation, always with `speed=1.0`. The `terra_primary_review` and `terra_synthesis` transition identifiers remain stable; select the model from `model_policy`, not the step name. The final `record_qa_task_outcome` receives the original `run_id`.

New outcome events use schema v2 stage counters: `triage_calls`, `primary_review_calls`, `deep_review_calls`, and `synthesis_calls`; stage-token fields use the `triage_*`, `primary_review_*`, and `synthesis_*` names, while deep-review tokens remain `deep_input_tokens` and `deep_output_tokens`. Stored v1 history remains readable and reported separately.

References: [official Cursor MCP documentation](https://docs.cursor.com/context/model-context-protocol) and [Cursor Rules documentation](https://cursor.com/docs/rules).
