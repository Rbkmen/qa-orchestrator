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
(`OpenAI / Codex` or `Anthropic / Claude`), then walks through
`provider → model → reasoning/effort` for triage, primary review, deep review,
and synthesis. The menu contains a short list of recommended model IDs and an
option to enter another exact ID. Enter an ID supported by that user's
account; the MCP cannot reliably expose every provider's live model catalog.
The wizard saves the model policy; it does not configure provider access in
Codex or Claude Code. Make sure the selected host can use the chosen provider
and model. It stores only these non-secret values in
`$HOME/.qa-orchestrator/model-policy.json` (or the path from
`QA_ORCHESTRATOR_MODEL_POLICY_PATH`). API keys are never requested or stored.
Inspect the result with:

```bash
uv run qa-orch config show
uv run qa-orch reload
```

For OpenAI, the recommended menu currently includes `gpt-6-astra`,
`gpt-6-sol`, `gpt-6-luna`, `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`,
and `gpt-4.1`. The wizard offers model-specific `reasoning.effort` values:
`gpt-6-astra` starts at `low`, while `gpt-4.1` uses `none` because it does
not support reasoning. Other exact OpenAI IDs, such as `gpt-5.5` or `gpt-5.4`,
can be entered through the custom-ID option; unsupported combinations are
rejected before saving.

For Anthropic, the recommended menu currently includes `claude-fable-5-1`,
`claude-opus-5-5`, `claude-opus-5`, `claude-sonnet-5`, and
`claude-haiku-4-5-20251001`. Supported models use Anthropic's
`output_config.effort`; Haiku 4.5 does not support effort and therefore uses
`none`. Here `none` is a local sentinel meaning that the provider-specific
parameter must be omitted. `high` remains the recommendation for complex and
risky deep checks when the selected model supports it. Use the exact ID
available in the selected account. See the [OpenAI model catalog](https://developers.openai.com/api/docs/models/all),
[OpenAI reasoning guide](https://developers.openai.com/api/docs/guides/reasoning),
and [Anthropic model documentation](https://platform.claude.com/docs/en/models/overview)
for current provider details.

`reload` re-reads and validates the saved policy. If an MCP client is already
connected, restart its MCP connection after changing the policy. The wizard
colors providers, model IDs, and reasoning values; use `NO_COLOR=1` to disable
colors or `FORCE_COLOR=1` to force them.

### Alternative: use GitHub without a checkout

On macOS or Linux, `uvx` can run the commands directly from GitHub. First,
save the model policy in your user configuration:

```bash
uvx --from git+https://github.com/Rbkmen/qa-orchestrator.git qa-orch setup
```

To inspect the saved policy or validate it again without a checkout, run:

```bash
uvx --from git+https://github.com/Rbkmen/qa-orchestrator.git qa-orch config show
uvx --from git+https://github.com/Rbkmen/qa-orchestrator.git qa-orch reload
```

Then register the MCP command in the client as shown below. The `qa-orchestrator`
command starts the STDIO server and should be launched by the MCP client, not
run by itself as a setup command. The Codex guide includes the `uvx`
registration command; the [Claude Code guide](clients/claude-code.md) includes
its equivalent. For a team setup, pin a release tag or commit instead of using
the default branch by appending `@<tag-or-commit>` to the Git URL. Replace the
placeholder and use the exact same pinned URL in both the one-time setup and
the MCP server command, for example:

```bash
uvx --from 'git+https://github.com/Rbkmen/qa-orchestrator.git@<tag-or-commit>' qa-orch setup
```

`uvx` supports Git sources and pinned refs; see the [uv
documentation](https://docs.astral.sh/uv/guides/tools/).

Optional maintainer checks (from the checkout):

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

If you use `uvx` without a checkout, register this command instead:

```bash
codex mcp add qa-orchestrator -- uvx \
  --from git+https://github.com/Rbkmen/qa-orchestrator.git \
  qa-orchestrator
codex mcp list
```

These commands save the MCP server in your local Codex configuration. The
ChatGPT desktop app, Codex CLI, and IDE extension share that configuration.
For the desktop UI steps and command/argument values for both installation
methods, see the [Codex setup guide](clients/codex.md). See the [official Codex
MCP guide](https://developers.openai.com/codex/mcp) for current UI and
configuration options.

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

If you installed with `uvx` and do not have a checkout, use the canonical
[Codex host instructions on GitHub](https://github.com/Rbkmen/qa-orchestrator/blob/main/client-rules/generic/QA_ORCHESTRATOR_INSTRUCTIONS.md)
from the same branch, tag, or commit as the server, then merge them into the
appropriate `AGENTS.md` or persistent instructions. For a pinned version,
replace `main` in the link with the matching tag or commit.

For Anthropic/Claude Code, use the [Claude Code setup guide](clients/claude-code.md).

## 4. Verify the connection

1. In Codex CLI, run `codex mcp list`; in Claude Code, run `claude mcp get qa-orchestrator` or `/mcp`. In the ChatGPT desktop app, check **Settings → MCP servers** or use `/mcp`.
2. Restart the client if needed, then confirm that all six tools are available: `prepare_review_route`, `start_qa_orchestration`, `advance_qa_orchestration`, `get_qa_orchestration`, `record_qa_task_outcome`, and `get_metrics_report`.
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

Restart the MCP client after updating so it starts the current launcher and
code.

For a no-checkout installation, update the Git ref in both the one-time setup
command and the MCP server command, rerun `qa-orch setup` from that ref, then
restart the client. Keep both commands on the same tag or commit.

## Troubleshooting

| Symptom | Check |
|---|---|
| `QA Orchestrator is not installed` | Run `uv sync` from the cloned repository and check that `scripts/qa-orchestrator` points to that checkout. |
| Server is missing from Codex | Run `codex mcp list` and `codex mcp get qa-orchestrator`; if the checkout moved, remove the stale entry with `codex mcp remove qa-orchestrator`, add it again using the current absolute path, then restart Codex. |
| Server is enabled but tools do not appear | Restart Codex and confirm the launcher exists and is executable. |
| Server is missing or disconnected in Claude Code | Run `claude mcp list`, `claude mcp get qa-orchestrator`, or `/mcp`; verify the command and arguments in the client guide. |
| `uvx` cannot fetch the GitHub source | Confirm `uv` and Git are installed and that this machine can reach GitHub; for a stable team setup, use a published tag or commit. |
| Python or dependency error | Confirm Python is 3.12+ and run `uv sync` from the repository root. |
