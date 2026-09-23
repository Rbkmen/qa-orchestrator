# Claude Code setup

Claude Code can run QA Orchestrator as a local STDIO MCP server. Use user scope for all projects or local scope for one project.

## Connection

Run this from the QA Orchestrator repository root:

```bash
claude mcp add --transport stdio --scope user qa-orchestrator -- \
  "$(pwd)/scripts/qa-orchestrator"
```

On POSIX systems, use `uvx` when a local checkout is not desired:

```bash
claude mcp add --transport stdio --scope user qa-orchestrator -- \
  uvx --from git+https://github.com/Rbkmen/qa-orchestrator.git qa-orchestrator
```

Pin a release tag or commit instead of the default branch for a team setup.

Configure the model policy once before connecting. With a checkout, run
`uv run qa-orch setup` from the repository. Without a checkout, run:

```bash
uvx --from git+https://github.com/Rbkmen/qa-orchestrator.git qa-orch setup
```

Choose a provider and models available to your Claude Code environment. The
wizard stores only the non-secret model policy locally; it does not install a
model provider or grant access to one, and it never asks for API keys. Use the
same Git branch, tag, or commit in the one-time setup command and the MCP
command.

Verify the connection with `claude mcp get qa-orchestrator`, `claude mcp list`, or `/mcp` inside Claude Code.

## Host-agent instructions

For a project without `CLAUDE.md`:

Run this command from the QA Orchestrator repository root:

```bash
cp "$(pwd)/client-rules/claude-code/CLAUDE.md" \
  /absolute/path/to/your-project/CLAUDE.md
```

If the file already exists, merge the rules manually. Claude Code remains the owner of evidence, analysis, findings, model calls, changes, and external writes. The orchestrator publishes six tools for routing, orchestration state, and aggregate task distribution; it returns only structured content-free data.

If you installed with `uvx` and have no checkout, use the [canonical Claude
Code instructions on GitHub](https://github.com/Rbkmen/qa-orchestrator/blob/main/client-rules/claude-code/CLAUDE.md)
from the same branch, tag, or commit as the server. Merge them into the
project's `CLAUDE.md`; do not replace existing project instructions.

Use the returned `model_policy` for triage, the selected roles, optional deep review, and synthesis; the default provider is OpenAI/Codex and each stage's model and reasoning are configurable, with `speed=1.0` for every stage. Deep reasoning defaults to `high` when the selected model supports it and can be selected in `qa-orch setup`. OpenAI uses `reasoning.effort`; Anthropic uses `output_config.effort`; `none` means omit the provider-specific parameter. The setup wizard does not contact provider APIs; verify model access and custom-ID reasoning/effort support with the host provider. The triage stage selects exactly one fixed bundle or one compatibility profile; the bundle order cannot be changed. For `ordinary_mr`, use `Faraday — Evidence Investigator` → `Code Reviewer` → `Test Analyzer`; after every primary-review role, pass its `completed_profile`, and on the last role you must also pass the structured boolean `risk_signals` object in the same call (use `{}` when none apply) so the orchestrator selects deep review or synthesis. Do not pass `risk_signals` on the later synthesis transition. After each stage, call `advance_qa_orchestration`; record the final result once with `record_qa_task_outcome` and the original `run_id`. Transition identifiers are model-neutral; select the model from `model_policy`, not the step name. Use model-neutral status labels: `Triage` → `Primary review` → optional `Deep review` → `Final synthesis` → `Host`.

At the final status, call `record_qa_task_outcome` with `task_type`, `outcome`, and the original `run_id` for orchestrated work. Without a `run_id`, records are not deduplicated; do not retry after an uncertain response because counts represent successful record calls, not verified unique tasks. Only task type and timestamp are saved for the aggregate distribution; the outcome is used to finalize the session but is not persisted. `get_metrics_report(days)` returns the total task count and counts by task type.

Reference: [official Claude Code MCP documentation](https://code.claude.com/docs/en/mcp).
