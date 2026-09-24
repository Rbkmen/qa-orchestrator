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

Configure the model policy once before using the server. With a checkout, run
`uv run qa-orch setup` from the repository. Without a checkout, run:

```bash
uvx --from git+https://github.com/Rbkmen/qa-orchestrator.git qa-orch setup
```

The wizard records the selected provider and model policy; it does not give
Codex access to a model provider. Select models available to your configured
Codex account and environment. Use the same branch, tag, or commit in the
setup command and the server command.

### Configure in the ChatGPT desktop app

Open **Settings → MCP servers → Add server**, choose **STDIO**, and enter
`qa-orchestrator` as the server name. For a checkout, set the command to the
absolute path of `scripts/qa-orchestrator` and leave arguments empty. Without a
checkout, set the command to `uvx` and use these arguments:

```text
--from
git+https://github.com/Rbkmen/qa-orchestrator.git
qa-orchestrator
```

Save the server and restart the app. The ChatGPT desktop app, Codex CLI, and
IDE extension share the same MCP configuration; the CLI commands above are an
alternative way to register the server.

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

Add the rules from [`client-rules/generic/QA_ORCHESTRATOR_INSTRUCTIONS.md`](../../client-rules/generic/QA_ORCHESTRATOR_INSTRUCTIONS.md) to persistent project instructions, or adapt them to your Codex rules. If you have no checkout, use the [same canonical file on GitHub](https://github.com/Rbkmen/qa-orchestrator/blob/main/client-rules/generic/QA_ORCHESTRATOR_INSTRUCTIONS.md), matching the branch, tag, or commit used for the server. Do not install a separate routing skill: the MCP server and these instructions are sufficient.

The linked generic file is canonical for orchestration mechanics. Workspace and repository rules should add only task-specific routing, safety, and output requirements.

Primary Codex remains the host and owner of evidence, decisions, and external actions. Use the five tools for orchestration: `start_qa_orchestration`, `advance_qa_orchestration`, `get_qa_orchestration`, `prepare_review_route`, and `finish_qa_orchestration`.

The model policy is host-owned and configured locally with `qa-orch setup`. The default provider is OpenAI/Codex; provider, model IDs, and provider-specific reasoning/effort for triage, primary review, deep review, and synthesis can be selected from the console. OpenAI uses `reasoning.effort`, Anthropic uses `output_config.effort`, and `none` means omit the provider parameter. The wizard does not contact provider APIs; verify model access and custom-ID reasoning/effort support with the host provider. Deep reasoning defaults to `high` when supported by the selected model. Execution speed and latency preferences are controlled by the user's host/provider settings; the orchestrator does not set or override them. Label stages by function only (`Triage`, `Primary review`, optional `Deep review`, `Final synthesis`); don't include model names or reasoning levels in status text. Transition identifiers are model-neutral; select the model from `model_policy`, not the step name. Send the orchestrator only structured signals; prompts, evidence, and model outputs remain in Codex.

During triage, select exactly one fixed bundle or one compatibility profile, never both. For the ordinary MR bundle, the order is `code_explorer` (`Faraday — Evidence Investigator`) → `code_reviewer` (`Code Reviewer`) → `pr_test_analyzer` (`Test Analyzer`). After every primary-review role, pass its identifier as `completed_profile`; on the last role, you must also pass the structured boolean `risk_signals` object in the same call (use `{}` when none apply) so the orchestrator can select deep review or final synthesis. Do not pass `risk_signals` on the later synthesis transition. Faraday is an internal profile name, not a separate external service or model. Show status by stage only: `Triage` → `Primary review` per role → optional `Deep review` → `Final synthesis` → `Host` final outcome; pass the original `run_id` to `finish_qa_orchestration`.

For a low-risk, one-repository change with one narrow concern, prefer one compatibility profile: `code_reviewer` for behavior/callers, `pr_test_analyzer` for test-only changes, `typescript_reviewer` for TypeScript-only changes, or `react_reviewer` for React-only changes. Keep one compact Evidence Packet with `E1`-style references and bounded `F-01` finding candidates; pass only the relevant sections to each role.

After synthesis, call `finish_qa_orchestration` with the original `run_id` and the host-owned final outcome. For an early stop, pass the same `partial` or `blocked` outcome already sent to `advance_qa_orchestration`. The call finalizes only the in-memory session; no task history or statistics are stored.

Reference: [official Codex MCP documentation](https://developers.openai.com/codex/mcp).
