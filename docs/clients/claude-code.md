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

Use the flow `gpt-5.6-luna/max` → `gpt-5.6-terra/medium` for the selected roles → optional `gpt-5.6-sol/high` → `gpt-5.6-terra/medium` synthesis, with `speed=1.0` for every stage. Luna selects exactly one fixed bundle or one compatibility profile; the bundle order cannot be changed. For `ordinary_mr`, use `Faraday — Evidence Investigator` → `Code Reviewer` → `Test Analyzer`; after every Terra role, pass its `completed_profile`, and after the last role pass `risk_signals` so the orchestrator selects Sol or synthesis. After each stage, call `advance_qa_orchestration`; record the final result once with `record_qa_task_outcome` and the original `run_id`.

Reference: [official Claude Code MCP documentation](https://code.claude.com/docs/en/mcp).
