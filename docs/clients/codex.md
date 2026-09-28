# Codex setup

Codex connects QA Orchestrator as a local STDIO MCP server. On macOS and Linux, the source launcher uses the project's `.venv`, the active `VIRTUAL_ENV`, or an installed `qa-orchestrator-mcp` from `PATH`. On Windows, use the installed `.venv\Scripts\qa-orchestrator-mcp.exe` entry point.

## Connection

Run this from the QA Orchestrator repository root:

```bash
codex mcp add qa-orchestrator -- "$(pwd)/scripts/qa-orchestrator"
codex mcp list
```

On Windows, run this from PowerShell at the repository root:

```powershell
codex mcp add qa-orchestrator -- "$PWD\.venv\Scripts\qa-orchestrator-mcp.exe"
codex mcp list
```

Without a repository checkout, use `uvx`. In PowerShell, enter:

```powershell
codex mcp add qa-orchestrator -- uvx --from 'git+https://github.com/Rbkmen/qa-orchestrator.git@<commit-sha>' qa-orchestrator-mcp
```

On macOS or Linux, enter:

```bash
codex mcp add qa-orchestrator -- uvx \
  --from 'git+https://github.com/Rbkmen/qa-orchestrator.git@<commit-sha>' \
  qa-orchestrator-mcp
```

Replace `<commit-sha>` with the full commit hash and use the same hash in the
setup and server commands. See the [installation guide](../INSTALLATION.md)
for details and the update procedure.

Configure the model policy once before using the server. With a checkout, run
`uv run qa-orch setup` from the repository. Without a checkout, run:

```bash
uvx --from 'git+https://github.com/Rbkmen/qa-orchestrator.git@<commit-sha>' qa-orch setup
```

The wizard records the selected provider and model policy; it does not give
Codex access to a model provider. Select models available to your configured
Codex account and environment. Use the same pinned ref in the
setup command and the server command.

### Configure in the ChatGPT desktop app

Open **Settings → MCP servers → Add server**, choose **STDIO**, and enter
`qa-orchestrator` as the server name. For a macOS or Linux checkout, set the
command to the absolute path of `scripts/qa-orchestrator` and leave arguments
empty. For a Windows checkout, set it to the absolute path of
`.venv\Scripts\qa-orchestrator-mcp.exe` and leave arguments empty. Without a
checkout, set the command to `uvx` and use these arguments:

```text
--from
git+https://github.com/Rbkmen/qa-orchestrator.git@<commit-sha>
qa-orchestrator-mcp
```

Save the server and restart the app. The ChatGPT desktop app, Codex CLI, and
IDE extension share the same MCP configuration; the CLI commands above are an
alternative way to register the server.

Or add it to `$HOME/.codex/config.toml` on macOS and Linux. On Windows, the
Codex CLI command above is simplest; the config file is under
`%USERPROFILE%\.codex\config.toml`.

```toml
[mcp_servers.qa-orchestrator]
command = "/absolute/path/to/qa-orchestrator/scripts/qa-orchestrator"
args = []
startup_timeout_sec = 30
tool_timeout_sec = 120
```

For Windows TOML, use an absolute path such as
`C:/Users/you/qa-orchestrator/.venv/Scripts/qa-orchestrator-mcp.exe` as the
command. Verify the registration with `codex mcp list` and restart Codex.

## Host-agent instructions

Add or merge the canonical [host-agent instructions](../../client-rules/generic/QA_ORCHESTRATOR_INSTRUCTIONS.md) in persistent Codex instructions. Without a checkout, open the same file on GitHub at the server's commit SHA by replacing `main` in the [link](https://github.com/Rbkmen/qa-orchestrator/blob/main/client-rules/generic/QA_ORCHESTRATOR_INSTRUCTIONS.md). These instructions own profile selection, stage transitions, escalation, and finalization; workspace and repository rules should add only local routing, safety, and output requirements. Do not install a separate routing skill.

Codex remains the host and owns evidence, decisions, and external actions. Follow the returned `model_policy` for each stage. Execution speed and latency preferences are controlled by the user's host/provider settings; the orchestrator does not set or override them. Prompts, evidence, and model outputs stay in Codex.

Reference: [official Codex MCP documentation](https://developers.openai.com/codex/mcp).
