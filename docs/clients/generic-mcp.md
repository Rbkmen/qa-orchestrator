# Generic MCP client setup

QA Orchestrator uses the standard MCP STDIO transport. A compatible client must start the command, exchange MCP messages through stdin/stdout, and expose the tools to the host agent.

## Configuration

```json
{
  "mcpServers": {
    "qa-orchestrator": {
      "command": "/absolute/path/to/qa-orchestrator/scripts/qa-orchestrator",
      "args": [],
      "env": {}
    }
  }
}
```

The exact file depends on the client. Use an absolute path to the launcher.

## Instructions

Copy or merge [`client-rules/generic/QA_ORCHESTRATOR_INSTRUCTIONS.md`](../../client-rules/generic/QA_ORCHESTRATOR_INSTRUCTIONS.md) into the client's persistent instructions. The host must obtain sources, analyze evidence, and perform external actions itself.

## Verification

1. Confirm that the server connects and exposes exactly six tools: `prepare_review_route`, `start_qa_orchestration`, `advance_qa_orchestration`, `get_qa_orchestration`, `record_qa_task_outcome`, and `get_metrics_report`.
2. Start orchestration: GPT-6 Luna (`gpt-6-luna`)/max selects a fixed bundle or one profile; GPT-6 Sol (`gpt-6-sol`)/medium executes roles one at a time in a fixed order, and the host passes `completed_profile` after each role. Keep `speed=1.0` for every model stage. Optional GPT-6 Sol/high and GPT-6 Sol/medium synthesis are available only after the final role. The `terra_primary_review` and `terra_synthesis` transition identifiers remain stable; select the model from `model_policy`, not the step name.
3. After every stage, send the orchestrator only a structured signal; keep evidence and outputs in the host agent.
4. Verify `read_only=true` and `host_owns_decisions=true`, then finish the orchestrated task with one `record_qa_task_outcome` call using its `run_id`.
5. Check aggregate counters through `get_metrics_report`.

New outcome events use schema v2 stage counters (`triage_calls`, `primary_review_calls`, `deep_review_calls`, and `synthesis_calls`) and stage-token fields; deep-review tokens remain `deep_input_tokens` and `deep_output_tokens`. Stored v1 history remains readable and reported separately.

For a normal MR, use `ordinary_mr`: `code_explorer` (`Faraday — Evidence Investigator`) → `code_reviewer` → `pr_test_analyzer`. The remaining fixed bundles and display names are listed in the [routing policy](../ORCHESTRATION_POLICY.md); Faraday is only an internal role name.
