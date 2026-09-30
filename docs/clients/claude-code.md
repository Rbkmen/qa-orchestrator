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

Replace `<commit-sha>` with the full commit hash and use the same hash in the
setup and server commands. See the [installation guide](../INSTALLATION.md)
for details and the update procedure.

Configure the model policy once before connecting. With a checkout, run
`uv run qa-orch setup` from the repository. Without a checkout, run:

```bash
uvx --from 'git+https://github.com/Rbkmen/qa-orchestrator.git@<commit-sha>' qa-orch setup
```

Choose a provider and models available to your Claude Code environment. The
wizard stores only the non-secret model policy locally; it does not install a
model provider or grant access to one, and it never asks for API keys. Use the
same pinned ref in the one-time setup command and the MCP command.

Verify the connection with `claude mcp get qa-orchestrator`, `claude mcp list`, or `/mcp` inside Claude Code.

## Host-agent instructions

For a project without `CLAUDE.md`, combine the shared host-agent instructions with the Claude Code-specific workflow. From the QA Orchestrator repository root, run:

```bash
cat client-rules/generic/QA_ORCHESTRATOR_INSTRUCTIONS.md \
  client-rules/claude-code/CLAUDE.md \
  > /absolute/path/to/your-project/CLAUDE.md
```

The generic file defines the shared evidence and finding contract; the Claude
file adds the client workflow. If `CLAUDE.md` already exists, merge both files
into it without replacing existing project instructions. Registering the MCP
server alone does not load host-agent rules.

If you installed with `uvx` and have no checkout, open both the [shared
instructions](https://github.com/Rbkmen/qa-orchestrator/blob/main/client-rules/generic/QA_ORCHESTRATOR_INSTRUCTIONS.md)
and the [Claude Code instructions](https://github.com/Rbkmen/qa-orchestrator/blob/main/client-rules/claude-code/CLAUDE.md)
at the server's commit SHA by replacing `main` in each link. Merge both into the
project's `CLAUDE.md`; do not replace existing project instructions.

Claude Code remains the host and owns evidence, decisions, model calls, and
external actions. Follow the returned `model_policy` for each stage. Execution speed and latency preferences are controlled by the user's host/provider settings; the orchestrator does not set or override them.

Reference: [official Claude Code MCP documentation](https://code.claude.com/docs/en/mcp).
