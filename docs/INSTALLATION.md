# Install and configure QA Orchestrator

QA Orchestrator is a local MCP server that provides fixed QA routing, bounded orchestration state, and content-free metrics. It runs over MCP STDIO when the host client starts it; no separate background service, database, or API key is required by the server.

The host client—not the orchestrator—runs the model stages, gathers evidence, and makes the final QA decision. Model access follows the host user's account and permissions.

## Requirements

- macOS or Linux;
- Git;
- Python 3.12 or newer;
- [`uv`](https://docs.astral.sh/uv/);
- Codex CLI/Desktop or Claude Code.

The current release supports native macOS and Linux. Native Windows is not
supported because the source launcher and metrics locking use POSIX facilities;
Windows users can run the server inside WSL2.

Before continuing, verify the local prerequisites:

```bash
python3 --version
uv --version
```

Python must be 3.12 or newer. If `uv` is not installed, follow the [official
installation guide](https://docs.astral.sh/uv/getting-started/installation/).

## 1. Get the repository and set up its environment

```bash
git clone https://github.com/Rbkmen/qa-orchestrator.git
cd qa-orchestrator
uv sync
uv run qa-orchestrator-doctor
uv run qa-orch setup
```

`uv sync` creates the project environment, installs the server and its dependencies, and provides the `qa-orchestrator` command in `.venv`. The repository launcher uses that environment automatically.

`qa-orchestrator-doctor` performs read-only local checks for the Python version,
installed dependencies, metrics-directory permissions, and the source launcher.
It does not contact external services or create files. Use `--json` in scripts.

`qa-orch setup` opens the local console wizard. It asks for the AI environment
(`OpenAI / Codex` or `Anthropic / Claude`) and the model ID for triage, primary
review, deep review, and synthesis. For each role, the menu offers the
current/default model or an option to enter another ID. Enter an ID supported
by that user's account; the MCP cannot reliably expose every provider's live
model catalog. It stores
only these non-secret values in `$HOME/.qa-orchestrator/model-policy.json` (or
the path from `QA_ORCHESTRATOR_MODEL_POLICY_PATH`). API keys are never
requested or stored. Inspect the result with:

Interactive order: provider → model for a role → reasoning for that model.
For Anthropic, the OpenAI-specific reasoning question is skipped. For OpenAI,
deep-review reasoning is configurable too, with `high` as the recommendation
for complex and risky checks.

For OpenAI the wizard includes common IDs such as `gpt-6-astra`, `gpt-6-sol`,
`gpt-6-luna`, `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-5.5`,
`gpt-5.4`, and `gpt-4.1`. It then offers `reasoning.effort` values from
`none` through `max` for triage, primary review, deep review, and synthesis;
`high` is the deep-review recommendation. For Anthropic it includes current
examples such as `claude-opus-5-5`, `claude-opus-5`, `claude-opus-4-8`,
`claude-sonnet-5`, `claude-sonnet-4-6`, and `claude-haiku-4-5-20251001`.
The exact ID must be available in the selected account; use the custom option
when a model is not in the menu. See the [OpenAI model catalog](https://developers.openai.com/api/docs/models/all),
[OpenAI reasoning guide](https://developers.openai.com/api/docs/guides/reasoning),
and [Anthropic model status](https://platform.claude.com/docs/en/about-claude/model-deprecations)
for current lists.

```bash
uv run qa-orch config show
uv run qa-orch reload
```

`reload` re-reads and validates the saved policy. If an MCP client is already
connected, restart its MCP connection after changing the policy. The wizard
colors providers, model IDs, and reasoning values; use `NO_COLOR=1` to disable
colors or `FORCE_COLOR=1` to force them.

### Optional: run from Git without a checkout

On a POSIX system with `uv`, an MCP client can start the published repository
directly through `uvx`:

```bash
uvx --from git+https://github.com/Rbkmen/qa-orchestrator.git qa-orchestrator
# one-time model policy setup
uvx --from git+https://github.com/Rbkmen/qa-orchestrator.git qa-orch setup
```

For a team or CI configuration, pin a release tag or commit instead of the
default branch. This command is suitable as the client command with arguments
`--from git+https://github.com/Rbkmen/qa-orchestrator.git qa-orchestrator`.

Optional maintainer checks:

```bash
uv run pytest -q
uv run ruff check .
```

## 2. Connect it to Codex

Run these commands from the repository root:

```bash
codex --version
codex mcp add qa-orchestrator -- "$(pwd)/scripts/qa-orchestrator"
codex mcp list
```

The command registers the executable launcher in your local Codex configuration. Codex CLI and the IDE extension share that configuration. If you use Codex Desktop, verify the server in the app's MCP/settings UI instead of assuming that a CLI registration is visible there. `codex mcp list` should show `qa-orchestrator` as enabled. Restart Codex if it was already open. See the [official Codex MCP documentation](https://developers.openai.com/codex/mcp) for CLI and `config.toml` options.

Each user must register the server in their own local Codex environment. Use
either the checkout launcher or the `uvx` command; MCP configuration is not
shared automatically between machines.

## 3. Add the host instructions

Registering the MCP server makes its tools available; persistent instructions tell the host when and how to use them. Add this rule to the applicable Codex `AGENTS.md` or persistent instructions, replacing the placeholder with the path to that user's checkout:

```text
For implementation-aware QA reviews, read and follow the canonical QA Orchestrator instructions at:
<path-to-qa-orchestrator>/client-rules/generic/QA_ORCHESTRATOR_INSTRUCTIONS.md
```

If the target `AGENTS.md` already exists, merge the rule instead of replacing the file. The linked file is the canonical source for orchestration stages, risk escalation, and metrics; keep workspace-specific routing and output requirements in the workspace rules.

For Anthropic/Claude Code, use the [Claude Code setup guide](clients/claude-code.md).

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
