# Install and configure QA Orchestrator

QA Orchestrator is a local MCP server that provides fixed QA routing, bounded orchestration state, and content-free metrics. It runs over MCP STDIO when the host client starts it; no separate background service, database, or API key is required by the server.

The host client—not the orchestrator—runs the model stages, gathers evidence, and makes the final QA decision. Model access follows the host user's account and permissions.

## Requirements

- macOS or Linux;
- Git;
- Python 3.12 or newer;
- [`uv`](https://docs.astral.sh/uv/);
- an MCP client that supports STDIO. The examples below use Codex.

## 1. Get the repository and set up its environment

```bash
git clone https://github.com/Rbkmen/qa-orchestrator.git
cd qa-orchestrator
uv sync
```

`uv sync` creates the project environment, installs the server and its dependencies, and provides the `qa-orchestrator` command in `.venv`. The repository launcher uses that environment automatically.

Optional maintainer checks:

```bash
uv run pytest -q
uv run ruff check .
```

## 2. Connect it to Codex

Run these commands from the repository root:

```bash
codex mcp add qa-orchestrator -- "$(pwd)/scripts/qa-orchestrator"
codex mcp list
```

The command registers the executable launcher in your local Codex configuration. The desktop app, CLI, and IDE extension use the same MCP configuration, so this setup is shared by those clients on the same host. `codex mcp list` should show `qa-orchestrator` as enabled. Restart Codex if it was already open. See the [official Codex MCP documentation](https://developers.openai.com/codex/mcp) for CLI and `config.toml` options.

Each colleague must clone the repository and register the server in their own local Codex environment. Cloning the repository does not install MCP configuration on another machine.

## 3. Add the host instructions

Registering the MCP server makes its tools available; persistent instructions tell the host when and how to use them. Add this rule to the applicable Codex `AGENTS.md` or persistent instructions, replacing the placeholder with the path to that user's checkout:

```text
For implementation-aware QA reviews, read and follow the canonical QA Orchestrator instructions at:
<path-to-qa-orchestrator>/client-rules/generic/QA_ORCHESTRATOR_INSTRUCTIONS.md
```

If the target `AGENTS.md` already exists, merge the rule instead of replacing the file. The linked file is the canonical source for orchestration stages, risk escalation, and metrics; keep workspace-specific routing and output requirements in the workspace rules.

For other clients, use the matching setup guide:

- [Claude Code](clients/claude-code.md)
- [Cursor](clients/cursor.md)
- [Generic MCP client](clients/generic-mcp.md)

## 4. Verify the connection

1. In Codex, run `codex mcp list` and confirm `qa-orchestrator` is enabled.
2. Restart Codex, then confirm that the six tools are available: `prepare_review_route`, `start_qa_orchestration`, `advance_qa_orchestration`, `get_qa_orchestration`, `record_qa_task_outcome`, and `get_metrics_report`.
3. For a read-only smoke check, call `prepare_review_route` with `agent_profile="code_explorer"`. It should return the Faraday evidence-investigator route with `read_only=true` and `host_owns_decisions=true`.

The server is started on demand by the MCP client. Do not start a second background server manually.

## Data and privacy

Evidence, source code, logs, prompts, model responses, and final QA decisions stay with the host agent. Active orchestration state is bounded and held in process memory. Aggregate metrics are written locally to `$HOME/.qa-orchestrator/metrics.jsonl` by default; set `QA_ORCHESTRATOR_DATA_DIR` to use another directory.

## Updating

From the checkout, first make sure you have no local changes you need to keep, then update and resynchronize:

```bash
git pull --ff-only
uv sync
```

Restart Codex after updating so the client starts the current launcher and code.

## Troubleshooting

| Symptom | Check |
|---|---|
| `QA Orchestrator is not installed` | Run `uv sync` from the cloned repository and check that `scripts/qa-orchestrator` points to that checkout. |
| Server is missing from Codex | Run `codex mcp list` and `codex mcp get qa-orchestrator`; if the checkout moved, remove the stale entry with `codex mcp remove qa-orchestrator`, add it again using the current absolute path, then restart Codex. |
| Server is enabled but tools do not appear | Restart Codex and confirm the launcher exists and is executable. |
| Python or dependency error | Confirm Python is 3.12+ and run `uv sync` from the repository root. |
