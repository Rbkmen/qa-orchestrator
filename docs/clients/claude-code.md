# Claude Code setup

Claude Code can run QA Orchestrator as a local STDIO MCP server. Use user scope for all projects or local scope for one project.

## Connection

```bash
claude mcp add --transport stdio --scope user qa-orchestrator -- \
  /absolute/path/to/qa-orchestrator/scripts/qa-orchestrator
```

Verify the connection with `claude mcp get qa-orchestrator`, `claude mcp list`, or `/mcp` inside Claude Code.

## Host-agent instructions

For a project without `CLAUDE.md`:

```bash
cp /absolute/path/to/qa-orchestrator/client-rules/claude-code/CLAUDE.md \
  /absolute/path/to/your-project/CLAUDE.md
```

If the file already exists, merge the rules manually. Claude Code remains the owner of evidence, analysis, findings, model calls, changes, and external writes. The orchestrator publishes six tools for routing, orchestration state, and aggregate metrics; it returns only structured content-free data.

Use the flow `gpt-6-luna/max` → `gpt-6-sol/medium` for the selected roles → optional `gpt-6-sol/high` → `gpt-6-sol/medium` synthesis, with `speed=1.0` for every stage. Luna selects exactly one fixed bundle or one compatibility profile; the bundle order cannot be changed. For `ordinary_mr`, use `Faraday — Evidence Investigator` → `Code Reviewer` → `Test Analyzer`; after every primary-review role, pass its `completed_profile`, and on the last role pass `risk_signals` in the same call so the orchestrator selects Sol deep review or synthesis. Do not pass `risk_signals` on the later synthesis transition. After each stage, call `advance_qa_orchestration`; record the final result once with `record_qa_task_outcome` and the original `run_id`. The `terra_primary_review` and `terra_synthesis` transition identifiers remain stable; select the model from `model_policy`, not the step name.

New outcome events use schema v2 stage counters: `triage_calls`, `primary_review_calls`, `deep_review_calls`, and `synthesis_calls`; stage-token fields use the `triage_*`, `primary_review_*`, and `synthesis_*` names, while deep-review tokens remain `deep_input_tokens` and `deep_output_tokens`. Stored v1 history remains readable and reported separately.

Reference: [official Claude Code MCP documentation](https://code.claude.com/docs/en/mcp).
