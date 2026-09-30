# Install and configure QA Orchestrator

QA Orchestrator is a local MCP server that provides fixed QA routing and bounded orchestration state. It runs over MCP STDIO when the host client starts it; no separate background service, database, or API key is required by the server.

The host client—not the orchestrator—runs the model stages, gathers evidence, and makes the final QA decision. Model access follows the host user's account and permissions.

## Requirements

- Windows, macOS, or Linux;
- Git;
- Python 3.12 or newer;
- [`uv`](https://docs.astral.sh/uv/);
- Codex CLI/Desktop or Claude Code.

The current release supports native Windows, macOS, and Linux. Windows uses
the installed `qa-orchestrator-mcp.exe` entry point; the source shell launcher
is for macOS and Linux.

On Windows PowerShell, verify the local prerequisites:

```powershell
python --version
uv --version
```

On macOS and Linux, use `python3 --version` and `uv --version`.

Python must be 3.12 or newer. If `uv` is not installed, follow the [official
installation guide](https://docs.astral.sh/uv/getting-started/installation/).

## 1. Get the repository and set up its environment

```bash
git clone https://github.com/Rbkmen/qa-orchestrator.git
cd qa-orchestrator
uv sync --locked
uv run qa-orchestrator-doctor
uv run qa-orch setup
```

`uv sync --locked` creates the project environment using the committed lockfile, installs the server and its
dependencies, and provides the `qa-orchestrator` and `qa-orchestrator-mcp`
commands in `.venv`. The repository launcher uses that environment
automatically on macOS and Linux.

`qa-orchestrator-doctor` performs read-only local checks for the Python version,
installed dependencies, model policy, optional SQLite session-store integrity,
and the platform-specific MCP launcher. It does not contact external services,
create files, change the session store, or report a session count. Use `--json`
in scripts.

`qa-orch setup` opens the local console wizard. First choose Russian or English
for this setup run; the language is not saved in `model-policy.json`. Then
choose the AI environment (`OpenAI / Codex` or `Anthropic / Claude`) and models
for triage, primary review, deep review, and synthesis. Each stage includes a
short explanation of its role. Choose the provider-specific reasoning/effort
for each model; the menu contains recommended model IDs and an option to enter
another exact ID. The wizard does not contact provider APIs or confirm account
access. Its recommended catalog and custom-ID reasoning options are local
guidance, not live capability checks; verify custom model availability and
reasoning/effort support with the host's provider.
The wizard saves the model policy; it does not configure provider access in
Codex or Claude Code. Make sure the selected host can use the chosen provider
and model. It stores only these non-secret values in the user's home file
`~/.qa-orchestrator/model-policy.json` (or the path from
`QA_ORCHESTRATOR_MODEL_POLICY_PATH`). API keys are never requested or stored.
Inspect the result with:

```bash
uv run qa-orch config show
uv run qa-orch reload
```

For OpenAI, the recommended menu currently includes `gpt-6-astra`,
`gpt-6.1-sol`, `gpt-6-sol`, `gpt-6-luna`, `gpt-5.6-sol`, `gpt-5.6-terra`,
`gpt-5.6-luna`, and `gpt-4.1`. The wizard offers model-specific
`reasoning.effort` values: `gpt-6-astra` and `gpt-6.1-sol` support `low`,
`medium`, `high`, `xhigh`, and `max`, but not `none` or `minimal`; `gpt-4.1`
uses `none` as a local sentinel because it does not support reasoning; the host
omits the parameter for that model. On reasoning models that support `none`,
the host must send it explicitly to disable reasoning: omitting the parameter
uses the model default instead. See the [OpenAI reasoning guide](https://developers.openai.com/api/docs/guides/reasoning). Other exact OpenAI IDs, such
as `gpt-5.5` or `gpt-5.4`, can be entered through the custom-ID option. The
wizard rejects unsupported reasoning values only for model IDs with locally
curated capability data.
For an unknown custom ID, it checks the ID format and provider hint but does
not verify the model's actual capabilities; an unsupported reasoning value
may still be saved. A returned `reasoning_capabilities_verified: false` means
you must confirm model access and reasoning support with the provider.

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
for current provider details. The compatibility entries for Claude Opus 4.6
and Sonnet 4.6 allow `low`, `medium`, `high`, and `max`, but not `xhigh`; see
the [Anthropic effort guide](https://platform.claude.com/docs/en/build-with-claude/effort).

`config show` reads the saved file (or defaults if it is missing). `reload`
validates it in a separate CLI process; it does not reload a connected server.
After changing the policy, restart the client's MCP server connection and call
`get_qa_orchestration_model_policy` with `{}`. That read-only MCP tool returns
the in-memory provider, four model IDs, and their reasoning levels used for new
sessions. It does not reread the file, start a session, contact the provider,
or prove which model the host actually executed. Use host execution details
for that last check.

If no policy exists, the server uses `gpt-6-luna` / `max` for triage and
`gpt-6-sol` with `medium`, `high`, and `medium` for primary review, deep review,
and synthesis respectively. Accepting defaults in setup preserves an existing
selection. The wizard
colors providers, model IDs, and reasoning values; use `NO_COLOR=1` to disable
colors or `FORCE_COLOR=1` to force them.

### Setup without prompts

Supply a provider and all four model IDs. Supply all four reasoning flags to
set them explicitly; if all reasoning flags are omitted, setup uses valid
defaults for each model and preserves compatible saved reasoning values.
For example, from a checkout:

```bash
uv run qa-orch setup --provider openai \
  --triage-model gpt-6-luna --triage-reasoning max \
  --primary-model gpt-6.1-sol --primary-reasoning medium \
  --deep-model gpt-6.1-sol --deep-reasoning high \
  --synthesis-model gpt-6.1-sol --synthesis-reasoning medium
```

This writes the local policy immediately. It does not change an already running
server. For PowerShell, enter the command on one line or replace shell
continuations with PowerShell backticks.

### Alternative: use GitHub without a checkout

`uvx` can run the commands directly from GitHub on all supported platforms.
First, replace `<commit-sha>` below with the full commit hash you want to use, then
save the model policy in your user configuration:

```bash
uvx --from 'git+https://github.com/Rbkmen/qa-orchestrator.git@<commit-sha>' qa-orch setup
```

To inspect the saved policy or validate it again without a checkout, run:

```bash
uvx --from 'git+https://github.com/Rbkmen/qa-orchestrator.git@<commit-sha>' qa-orch config show
uvx --from 'git+https://github.com/Rbkmen/qa-orchestrator.git@<commit-sha>' qa-orch reload
```

Then register the MCP command in the client as shown below. The
`qa-orchestrator-mcp` command starts the STDIO server and should be launched
by the MCP client, not run by itself as a setup command. The Codex guide
includes the `uvx` registration command; the [Claude Code guide](clients/claude-code.md) includes
its equivalent. For reproducible team setup, pin an immutable commit SHA
instead of a moving branch. Use the exact same pinned URL in the one-time
setup and the MCP server command shown above.

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

On Windows, run the registration from PowerShell at the repository root:

```powershell
codex --version
codex mcp add qa-orchestrator -- "$PWD\.venv\Scripts\qa-orchestrator-mcp.exe"
codex mcp list
```

If you use `uvx` without a checkout, register this command instead:

```bash
codex mcp add qa-orchestrator -- uvx \
  --from 'git+https://github.com/Rbkmen/qa-orchestrator.git@<commit-sha>' \
  qa-orchestrator-mcp
codex mcp list
```

These commands save the MCP server in your local Codex configuration. The
ChatGPT desktop app, Codex CLI, and IDE extension share that configuration.
For the desktop UI steps and command/argument values for both installation
methods, see the [Codex setup guide](clients/codex.md). See the [official Codex
MCP guide](https://developers.openai.com/codex/mcp) for current UI and
configuration options.

Each user must register the server in their own local Codex environment. Use
either the platform's checkout entry point or the `uvx` command. MCP
configuration is not shared automatically between machines.

## 3. Add the host instructions

Registering the MCP server makes its tools available; persistent instructions tell the host when and how to use them. Add this rule to the applicable Codex `AGENTS.md` or persistent instructions, replacing the placeholder with the path to that user's checkout:

```text
For implementation-aware QA reviews, read and follow the canonical QA
Orchestrator instructions at:
<path-to-qa-orchestrator>/client-rules/generic/QA_ORCHESTRATOR_INSTRUCTIONS.md
```

If the target `AGENTS.md` already exists, merge the rule instead of replacing the file. The linked file is the canonical source for orchestration mechanics; keep workspace-specific routing and output requirements in the workspace rules.

If you installed with `uvx` and do not have a checkout, use the canonical
[Codex host instructions on GitHub](https://github.com/Rbkmen/qa-orchestrator/blob/main/client-rules/generic/QA_ORCHESTRATOR_INSTRUCTIONS.md)
from the same commit as the server, then merge them into the
appropriate `AGENTS.md` or persistent instructions. For a pinned version,
replace `main` in the link with the matching commit SHA.

For Anthropic/Claude Code, use the [Claude Code setup guide](clients/claude-code.md).

## 4. Verify the connection

1. In Codex CLI, run `codex mcp list`; in Claude Code, run `claude mcp get qa-orchestrator` or `/mcp`. In the ChatGPT desktop app, check **Settings → MCP servers** or use `/mcp`.
2. Restart the client if needed, then confirm that all nine tools are available: `prepare_qa_orchestration`, `get_qa_orchestration_catalog`, `get_qa_orchestration_model_policy`, `start_qa_orchestration`, `advance_qa_orchestration`, `get_qa_orchestration`, `list_qa_orchestrations`, `finish_qa_orchestration`, and `delete_qa_orchestration`.
3. For a read-only smoke check, call `prepare_qa_orchestration` with `agent_profile="code_explorer"`. It should return the Faraday evidence-investigator route with `read_only=true` and `host_owns_decisions=true`.
4. Call `get_qa_orchestration_model_policy` with `{}` and compare the returned selection with `qa-orch config show`. These checks create no QA session. Registration in `mcp list` alone does not prove a successful tool call.
5. Call `list_qa_orchestrations` with `{}` to discover retained, non-expired session IDs. An empty `sessions` list is valid. To recover a lost ID, identify the intended entry and call `get_qa_orchestration` with its `run_id`; confirm which session to resume if several match. Listing does not extend TTL or write storage.
6. Call `get_qa_orchestration_catalog` with `{}` to discover profiles, bundle purposes and ordered routes, and task-type recommendation shortlists before creating a session or choosing a triage route. This read-only call creates no session and changes no state or TTL.

For an intentionally discarded session, call `delete_qa_orchestration` with its
`run_id`. The call removes its memory and recovery record irreversibly;
`deleted=true` also succeeds for an already missing ID. It does not record a QA
outcome or stop work executing in the host. Use `finish_qa_orchestration` for
normal finalization; do not delete existing sessions as part of a smoke check.

The server is started on demand by the MCP client. Do not start a second background server manually.

The server disables FastMCP's startup banner and its automatic package-update
check. Normal STDIO startup and QA tool calls need no network. Initial
dependency installation, GitHub-based `uvx` installation, and host evidence/model
access can still require network connectivity.

## Data and privacy

Evidence, source code, logs, prompts, model responses, and final QA decisions stay with the host agent. Active orchestration state is bounded and held in process memory by default. To resume unfinished orchestration after a server restart, optionally set `QA_ORCHESTRATOR_SESSION_STORE_PATH` to a local SQLite file. That file holds only current structured session state; finalization deletes the session's row and keeps the database file. It does not contain task evidence, content, finalized outcomes, history, or statistics. Use one server process per store file. Without that setting, no session state is written to disk. The model policy is stored at `$HOME/.qa-orchestrator/model-policy.json` by default; set `QA_ORCHESTRATOR_DATA_DIR` or `QA_ORCHESTRATOR_MODEL_POLICY_PATH` to change that location. It contains provider, model IDs, and reasoning/effort settings, not credentials or task data.

### Configuration overrides

The [configuration variables](../README.md#configuration) must reach the server
through the MCP client's environment. An environment variable set only in a
terminal may not reach a desktop client. Use the same policy path for setup
and the server. For Codex, an existing TOML entry can include:

```toml
[mcp_servers.qa-orchestrator]
command = "/absolute/path/to/qa-orchestrator/scripts/qa-orchestrator"
args = []
env = { QA_ORCHESTRATOR_MODEL_POLICY_PATH = "/absolute/path/to/model-policy.json", QA_ORCHESTRATOR_ORCHESTRATION_TTL_SECONDS = "1800", QA_ORCHESTRATOR_ORCHESTRATION_MAX_SESSIONS = "100" }
```

Use the Windows entry point or pinned `uvx` command from the client guide when
applicable. Run setup with that same `QA_ORCHESTRATOR_MODEL_POLICY_PATH` in
your terminal. Add `QA_ORCHESTRATOR_SESSION_STORE_PATH` only for optional
recovery. Keep the file local and use one server process per file; multiple
clients need separate store paths. On POSIX, the server uses private file
permissions; on Windows, restrict access using the directory's ACLs.

Recovered sessions retain the policy returned for their current stage. Their
next successful transition uses the policy loaded by the restarted server.
`get_qa_orchestration(run_id)` is authoritative for an existing session;
`get_qa_orchestration_model_policy` describes the current server's policy.

## Updating

From the checkout, first make sure you have no local changes you need to keep, then update and resynchronize:

```bash
git pull --ff-only
uv sync --locked
```

Restart the MCP client after updating so it starts the current launcher and
code.

The profile lookup tool is now named `prepare_qa_orchestration`. Replace
`prepare_review_route` in any pinned host instructions or direct tool calls;
the old MCP tool name is no longer published.

For a no-checkout installation, choose the new commit SHA and use it in both
the one-time setup command and the MCP server command. Rerun `qa-orch setup`
from that commit, then update the existing MCP registration and restart the
client. For Codex CLI, remove and re-add the entry:

```bash
uvx --from 'git+https://github.com/Rbkmen/qa-orchestrator.git@<commit-sha>' qa-orch setup
codex mcp remove qa-orchestrator
codex mcp add qa-orchestrator -- uvx \
  --from 'git+https://github.com/Rbkmen/qa-orchestrator.git@<commit-sha>' \
  qa-orchestrator-mcp
```

For Claude Code, remove and re-add the entry with the same pinned URL:

```bash
claude mcp remove qa-orchestrator
claude mcp add --transport stdio --scope user qa-orchestrator -- \
  uvx --from 'git+https://github.com/Rbkmen/qa-orchestrator.git@<commit-sha>' \
  qa-orchestrator-mcp
```

If you registered the server in a desktop UI or edited TOML directly, update
that existing entry instead of adding a second one. Keep the model setup and
server command on the same commit.

## Troubleshooting

| Symptom | Check |
|---|---|
| `QA Orchestrator is not installed` | Run `uv sync` from the cloned repository; use `scripts/qa-orchestrator` on macOS/Linux or `.venv\Scripts\qa-orchestrator-mcp.exe` on Windows. |
| Server is missing from Codex | Run `codex mcp list` and `codex mcp get qa-orchestrator`; if the checkout moved, remove the stale entry with `codex mcp remove qa-orchestrator`, add it again using the current absolute path, then restart Codex. |
| Server is enabled but tools do not appear | Restart Codex and confirm the configured platform entry point exists. |
| Models differ between the CLI and MCP | Confirm both use the same policy path. Restart the server connection, then call `get_qa_orchestration_model_policy` again. `reload` alone does not update the connected server. |
| The host uses a different model | The MCP returns instructions, not execution telemetry. Verify model selection in the host; report unavailable stage settings explicitly. |
| Invalid model policy or session store | Run `qa-orchestrator-doctor --json`. Correct the policy with setup; preserve a broken recovery store for diagnosis and configure a new store path if you need a fresh start. |
| Server is missing or disconnected in Claude Code | Run `claude mcp list`, `claude mcp get qa-orchestrator`, or `/mcp`; verify the command and arguments in the client guide. |
| `uvx` cannot fetch the GitHub source | Confirm `uv` and Git are installed and that this machine can reach GitHub; for a reproducible team setup, pin an immutable commit SHA. |
| Python or dependency error | Confirm Python is 3.12+ and run `uv sync` from the repository root. |
