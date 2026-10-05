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

Replace `<commit-sha>` with the full commit hash. If you later configure explicit models with `uvx`, use the same pinned ref for setup and the server. See the [installation guide](../INSTALLATION.md) for the update procedure.

No model setup is needed before first use. Without a saved policy, `host_default` uses the model and reasoning already configured in Codex. To use explicit per-stage settings, run `uv run qa-orch setup` from a checkout or the matching pinned `uvx` command; return to host settings with `qa-orch setup --host-default`. The wizard records settings but does not grant Codex access to a provider.

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

FastMCP sends concise advisory instructions in MCP initialization to clients that surface them. The full canonical [host-agent instructions](../../client-rules/generic/QA_ORCHESTRATOR_INSTRUCTIONS.md) include the shared evidence, severity, affected-area, and final-finding contract. Add a reference to them in persistent Codex instructions when you want the full contract kept in scope; this is not required for registration. Without a checkout, open the same file on GitHub at the server's commit SHA by replacing `main` in the [link](https://github.com/Rbkmen/qa-orchestrator/blob/main/client-rules/generic/QA_ORCHESTRATOR_INSTRUCTIONS.md). Workspace and repository rules may add local routing, safety, and domain-specific details, but must preserve the shared evidence and finding standards. Do not install a separate routing skill.

Codex remains the host and owns evidence, decisions, and external actions. Follow the returned `model_policy` for each stage. Execution speed and latency preferences are controlled by the user's host/provider settings; the orchestrator does not set or override them. Prompts, evidence, and model outputs stay in Codex.

Reference: [official Codex MCP documentation](https://developers.openai.com/codex/mcp).
