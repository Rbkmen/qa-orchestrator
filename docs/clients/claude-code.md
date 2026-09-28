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
  uvx --from 'git+https://github.com/Rbkmen/qa-orchestrator.git@<commit-sha>' \
  qa-orchestrator
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

For a project without `CLAUDE.md`:

Run this command from the QA Orchestrator repository root:

```bash
cp "$(pwd)/client-rules/claude-code/CLAUDE.md" \
  /absolute/path/to/your-project/CLAUDE.md
```

If the file already exists, merge the rules manually. The Claude-specific
instruction file is the complete host workflow; keep this setup guide focused
on installation and use workspace or repository rules for local requirements.

If you installed with `uvx` and have no checkout, open the [canonical Claude
Code instructions on GitHub](https://github.com/Rbkmen/qa-orchestrator/blob/main/client-rules/claude-code/CLAUDE.md)
at the server's commit SHA by replacing `main` in the link. Merge them into the
project's `CLAUDE.md`; do not replace existing project instructions.

Claude Code remains the host and owns evidence, decisions, model calls, and
external actions. Follow the returned `model_policy` for each stage.
Execution speed and latency preferences are controlled by the user's
host/provider settings; the orchestrator does not set or override them.

Reference: [official Claude Code MCP documentation](https://code.claude.com/docs/en/mcp).
