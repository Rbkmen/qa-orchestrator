# Generic MCP client setup

QA Router uses the standard MCP STDIO transport. A compatible client must start the command, exchange MCP messages through stdin/stdout, and expose the tools to the host agent.

## Configuration

```json
{
  "mcpServers": {
    "qa-router": {
      "command": "/absolute/path/to/qa-router-mcp/scripts/qa-router-mcp",
      "args": [],
      "env": {}
    }
  }
}
```

The exact file depends on the client. Use an absolute path to the launcher.

## Instructions

Copy or merge [`client-rules/generic/QA_ROUTER_INSTRUCTIONS.md`](../../client-rules/generic/QA_ROUTER_INSTRUCTIONS.md) into the client's persistent instructions. The host must obtain sources, analyze evidence, and perform external actions itself.

## Verification

1. Confirm that the server connects and exposes exactly six tools: `prepare_review_route`, `start_qa_orchestration`, `advance_qa_orchestration`, `get_qa_orchestration`, `record_qa_task_outcome`, and `get_metrics_report`.
2. Start orchestration: Luna/max selects a fixed bundle or one profile; Terra/medium executes roles one at a time in a fixed order, and the host passes `completed_profile` after each role. Optional Sol/high and Terra/medium synthesis are available only after the final role.
3. After every stage, send the router only a structured signal; keep evidence and outputs in the host agent.
4. Verify `read_only=true` and `host_owns_decisions=true`, then finish the orchestrated task with one `record_qa_task_outcome` call using its `run_id`.
5. Check aggregate counters through `get_metrics_report`.

For a normal MR, use `ordinary_mr`: `code_explorer` (`Faraday — Evidence Investigator`) → `code_reviewer` → `pr_test_analyzer`. The remaining fixed bundles and display names are listed in the [routing policy](../ROUTING_POLICY.md); Faraday is only an internal role name.
