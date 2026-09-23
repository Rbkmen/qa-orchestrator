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

If the file already exists, merge the rules manually. Claude Code remains the owner of evidence, analysis, findings, model calls, changes, and external writes. The orchestrator publishes six tools for routing, orchestration state, and aggregate metrics; it returns only structured content-free data.

If you installed with `uvx` and have no checkout, use the [canonical Claude
Code instructions on GitHub](https://github.com/Rbkmen/qa-orchestrator/blob/main/client-rules/claude-code/CLAUDE.md)
from the same branch, tag, or commit as the server. Merge them into the
project's `CLAUDE.md`; do not replace existing project instructions.

Use the returned `model_policy` for triage, the selected roles, optional deep review, and synthesis; the default provider is OpenAI/Codex and each stage's model and reasoning are configurable, with `speed=1.0` for every stage. Deep reasoning defaults to `high` when the selected model supports it and can be selected in `qa-orch setup`. OpenAI uses `reasoning.effort`; Anthropic uses `output_config.effort`; `none` means omit the provider-specific parameter. The triage stage selects exactly one fixed bundle or one compatibility profile; the bundle order cannot be changed. For `ordinary_mr`, use `Faraday — Evidence Investigator` → `Code Reviewer` → `Test Analyzer`; after every primary-review role, pass its `completed_profile`, and on the last role pass `risk_signals` in the same call so the orchestrator selects deep review or synthesis. Do not pass `risk_signals` on the later synthesis transition. After each stage, call `advance_qa_orchestration`; record the final result once with `record_qa_task_outcome` and the original `run_id`. The `terra_primary_review` and `terra_synthesis` transition identifiers remain stable; select the model from `model_policy`, not the step name. Use model-neutral status labels: `Triage` → `Primary review` → optional `Deep review` → `Final synthesis` → `Host`.

New outcome events use schema v2 stage counters: `triage_calls`, `primary_review_calls`, `deep_review_calls`, and `synthesis_calls`; stage-token fields use the `triage_*`, `primary_review_*`, and `synthesis_*` names, while deep-review tokens remain `deep_input_tokens` and `deep_output_tokens`. Stored v1 history remains readable and reported separately.

Reference: [official Claude Code MCP documentation](https://code.claude.com/docs/en/mcp).
