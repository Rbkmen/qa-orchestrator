# Codex setup

Codex connects QA Orchestrator as a local STDIO MCP server. The launcher uses the project's `.venv`, the active `VIRTUAL_ENV`, or an installed `qa-orchestrator` from `PATH`.

## Connection

Run this from the QA Orchestrator repository root:

```bash
codex mcp add qa-orchestrator -- "$(pwd)/scripts/qa-orchestrator"
codex mcp list
```

On POSIX systems, the repository checkout is optional:

```bash
codex mcp add qa-orchestrator -- uvx \
  --from git+https://github.com/Rbkmen/qa-orchestrator.git \
  qa-orchestrator
```

Pin a release tag or commit instead of the default branch for a team setup.

Or add it to `$HOME/.codex/config.toml`:

```toml
[mcp_servers.qa-orchestrator]
command = "/absolute/path/to/qa-orchestrator/scripts/qa-orchestrator"
args = []
startup_timeout_sec = 30
tool_timeout_sec = 120
```

For the TOML option, replace the placeholder with the absolute path to your
own checkout. Verify the registration with `codex mcp list` and restart Codex.

## Host-agent instructions

Add the rules from [`client-rules/generic/QA_ORCHESTRATOR_INSTRUCTIONS.md`](../../client-rules/generic/QA_ORCHESTRATOR_INSTRUCTIONS.md) to persistent project instructions, or adapt them to your Codex rules. Do not install a separate routing skill: the MCP server and these instructions are sufficient.

The linked generic file is canonical for orchestration mechanics. Workspace and repository rules should add only task-specific routing, safety, and output requirements.

Primary Codex remains the host and owner of evidence, decisions, and external actions. Use the six tools for orchestration: `start_qa_orchestration`, `advance_qa_orchestration`, `get_qa_orchestration`, `prepare_review_route`, `record_qa_task_outcome`, and `get_metrics_report`.

The model policy is host-owned and configured locally with `qa-orch setup`. The default is `gpt-6-luna/max` for triage, `gpt-6-sol/medium` for primary review and synthesis, and optional `gpt-6-sol/high` for read-only deep analysis; OpenAI or Anthropic, model IDs, and OpenAI reasoning effort for triage, primary review, deep review, and synthesis can be selected from the console. Deep reasoning defaults to `high`. Keep `speed=1.0` for every stage. The `terra_primary_review` and `terra_synthesis` transition identifiers remain stable; select the model from `model_policy`, not the step name. Send the orchestrator only structured signals; prompts, evidence, and model outputs remain in Codex.

During Luna triage, select exactly one fixed bundle or one compatibility profile, never both. For the ordinary MR bundle, the order is `code_explorer` (`Faraday — Evidence Investigator`) → `code_reviewer` (`Code Reviewer`) → `pr_test_analyzer` (`Test Analyzer`). After every primary-review role, pass its identifier as `completed_profile`; on the last role, pass `risk_signals` in the same call so the orchestrator can select Sol deep review or synthesis. Do not pass `risk_signals` on the later synthesis transition. Faraday is an internal profile name, not a separate external service or model. Show statuses as `Luna / Max` → `Sol / Medium` per role → optional `Sol / High` → `Sol / Medium` synthesis → `Host` final outcome; pass the original `run_id` to the final `record_qa_task_outcome`.

For a low-risk, one-repository change with one narrow concern, prefer one compatibility profile: `code_reviewer` for behavior/callers, `pr_test_analyzer` for test-only changes, `typescript_reviewer` for TypeScript-only changes, or `react_reviewer` for React-only changes. Keep one compact Evidence Packet with `E1`-style references and bounded `F-01` finding candidates; pass only the relevant sections to each role.

New outcome events use schema v2 stage counters: `triage_calls`, `primary_review_calls`, `deep_review_calls`, and `synthesis_calls`; stage-token fields use the `triage_*`, `primary_review_*`, and `synthesis_*` names, while deep-review tokens remain `deep_input_tokens` and `deep_output_tokens`. Stored v1 history remains readable and reported separately.

Reference: [official Codex MCP documentation](https://developers.openai.com/codex/mcp).
