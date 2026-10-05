# Claude Code setup

Claude Code can run QA Orchestrator as a local STDIO MCP server. Use user scope for all projects or local scope for one project.

## Connection

Run this from the QA Orchestrator repository root:

```bash
claude mcp add --transport stdio --scope user qa-orchestrator -- \
  "$(pwd)/scripts/qa-orchestrator"
```

On Windows, run this from PowerShell at the repository root:

```powershell
claude mcp add --transport stdio --scope user qa-orchestrator -- `
  "$PWD\.venv\Scripts\qa-orchestrator-mcp.exe"
```

When a local checkout is not desired, use `uvx`. In PowerShell, enter:

```powershell
claude mcp add --transport stdio --scope user qa-orchestrator -- uvx --from 'git+https://github.com/Rbkmen/qa-orchestrator.git@<commit-sha>' qa-orchestrator-mcp
```

On macOS or Linux, enter:

```bash
claude mcp add --transport stdio --scope user qa-orchestrator -- \
  uvx --from 'git+https://github.com/Rbkmen/qa-orchestrator.git@<commit-sha>' \
  qa-orchestrator-mcp
```

Replace `<commit-sha>` with the full commit hash. If you later configure explicit models with `uvx`, use the same pinned ref for setup and the server. See the [installation guide](../INSTALLATION.md) for the update procedure.

No model setup is needed before first use. Without a saved policy, `host_default` uses the model and reasoning already configured in Claude Code. To use explicit per-stage settings, run `uv run qa-orch setup` from a checkout or the matching pinned `uvx` command; return to host settings with `qa-orch setup --host-default`. The wizard stores only non-secret settings and does not grant Claude Code access to a provider.

Verify the connection with `claude mcp get qa-orchestrator`, `claude mcp list`, or `/mcp` inside Claude Code.

## Host-agent instructions

MCP initialization supplies concise advisory rules to clients that surface them. To keep the full evidence contract and Claude Code workflow in scope across tasks, optionally add persistent project instructions. From the QA Orchestrator repository root, combine:

```bash
cat client-rules/generic/QA_ORCHESTRATOR_INSTRUCTIONS.md \
  client-rules/claude-code/CLAUDE.md \
  > /absolute/path/to/your-project/CLAUDE.md
```

The generic file defines the shared evidence and finding contract; the Claude
file adds the client workflow. If `CLAUDE.md` already exists, merge both files
into it without replacing existing project instructions. MCP initialization
also carries concise advisory rules for clients that surface them; persistent
host rules keep the full contract in scope.

If you installed with `uvx` and have no checkout, open both the [shared
instructions](https://github.com/Rbkmen/qa-orchestrator/blob/main/client-rules/generic/QA_ORCHESTRATOR_INSTRUCTIONS.md)
and the [Claude Code instructions](https://github.com/Rbkmen/qa-orchestrator/blob/main/client-rules/claude-code/CLAUDE.md)
at the server's commit SHA by replacing `main` in each link. Merge both into the
project's `CLAUDE.md`; do not replace existing project instructions.

Claude Code remains the host and owns evidence, decisions, model calls, and
external actions. Follow the returned `model_policy` for each stage. Execution speed and latency preferences are controlled by the user's host/provider settings; the orchestrator does not set or override them.

Reference: [official Claude Code MCP documentation](https://code.claude.com/docs/en/mcp).
