# Contributing to QA Orchestrator

QA Orchestrator is a deterministic MCP service for fixed routing and bounded,
content-free state. The host owns evidence, model execution, and QA decisions.

## Set up development

Requirements: Git, Python 3.12+, and [uv](https://docs.astral.sh/uv/).
An MCP client is useful for manual verification; automated tests use FastMCP's
client and the real STDIO launcher without provider credentials.

```bash
git clone https://github.com/Rbkmen/qa-orchestrator.git
cd qa-orchestrator
uv sync --locked
uv run qa-orchestrator-doctor --json
uv run pytest -q
uv run ruff check .
```

For end-user registration and host rules, follow the
[installation guide](docs/INSTALLATION.md). `qa-orch setup` writes user
configuration; use a temporary `QA_ORCHESTRATOR_DATA_DIR` when experimenting.
If `QA_ORCHESTRATOR_MODEL_POLICY_PATH` is set, it takes precedence over that
directory. Never use production session-store files in development tests.

## Architecture map

| Module | Responsibility |
|---|---|
| `contracts.py` | Public task, outcome, profile, bundle, and route types |
| `review_profiles.py` | Fixed checklists, bundle order, task-based recommendations |
| `model_policy.py` | Local catalog, model/effort validation, atomic policy storage |
| `orchestration.py` | State machine, fixed escalation rules, TTL and capacity |
| `session_store.py` | Optional local recovery of unfinished structured sessions |
| `service.py` | Service facade joining configuration and orchestration |
| `server.py` | MCP schemas, descriptions, annotations, and tool registration |
| `config.py` | Environment-based limits and file locations |
| `cli.py` | Setup wizard, saved-policy inspection, validation, and console entry |
| `mcp_launcher.py`, `scripts/qa-orchestrator` | Installed and source launchers |
| `doctor.py` | Read-only local installation and recovery-store checks |

## Keep the responsibility boundary

- The host obtains sources, builds the Evidence Packet, runs model stages,
  validates findings, and makes the final decision.
- Profile preparation returns static metadata. Session tools accept fixed
  identifiers and structured signals, never evidence, prompts, source, logs,
  paths, arbitrary reasons, or model output.
- Model IDs and reasoning are instructions to the host. The server does not
  invoke providers, switch the host's model, or verify account access. Execution
  speed and latency preferences remain controlled by the user's host/provider
  settings.
- The loaded model-policy tool returns the server's in-memory selection for
  new sessions. Preserve its read-only, idempotent behavior and zero-argument
  schema. CLI `config show` and `reload` inspect disk in a separate process.
- Local state transitions are mutations even though review routes carry
  `read_only=true`. Keep MCP annotations consistent with observable behavior.
- Memory-only operation is the default. Optional SQLite recovery stores only
  unfinished structured sessions; finalization deletes their rows. Never add
  task history, statistics, persistent QA memory, a source cache, hidden external
  calls, or another autonomous agent.
- Invalid operations must preserve session state. Persistence failure must
  not acknowledge a transition that was not saved. Reads must not refresh TTL.

## Change a contract deliberately

The public surface has nine tools:

1. `prepare_qa_orchestration` — profile metadata;
2. `get_qa_orchestration_model_policy` — loaded server policy;
3. `start_qa_orchestration` — create a session;
4. `advance_qa_orchestration` — validated transition;
5. `get_qa_orchestration` — current session state for recovery;
6. `list_qa_orchestrations` — recover retained, non-expired session identifiers;
7. `finish_qa_orchestration` — finalize the host-owned outcome;
8. `get_qa_orchestration_catalog` — fixed profile and bundle discovery before triage;
9. `delete_qa_orchestration` — explicitly discard one session from memory and recovery storage.

Update the affected types, state machine, service, and server together. Match
the exact tool set, input/output schemas, annotations, descriptions, and real
MCP behavior in `tests/test_server.py`; verify the installed/source launcher
in `tests/test_install_artifacts.py`. Keep the README, installation guide,
[routing policy](docs/ORCHESTRATION_POLICY.md), and client rules consistent.

For a behavior fix, first add a regression test that reproduces the failure.
Cover invalid input, prohibited transitions, duplicate/stale calls, limits,
expiry, and data boundaries where they are affected. Assert observable
results and use real local files or MCP calls instead of checking only prose
or mocking the method under test.

For catalog changes, verify exact model IDs and effort options against provider
documentation. Update capability assertions, both wizard languages, and the
catalog review date only after checking its cited source. Preserve custom IDs;
`reasoning_capabilities_verified` means locally curated metadata, not a live
provider check. Do not require a provider API key to run setup or tests.

## Verification before review

Run from the repository root:

```bash
uv lock --check
uv run pytest -q
uv run ruff check .
uv run qa-orchestrator-doctor --json
uv build --wheel --out-dir dist
git diff --check
git status --short
```

On macOS/Linux, also run `sh -n scripts/qa-orchestrator`. Keep `dist/`, virtual
environments, caches, databases, and derived `.codegraph/` indexes out of Git.
Use Ruff's formatter on changed Python files; CI currently gates lint, not a
repository-wide format check.

| Area | Relevant tests |
|---|---|
| Profiles and fixed bundles | `test_review_profiles.py`, `test_service.py` |
| Routing, escalation, TTL, capacity, finalization | `test_orchestration.py`, `test_deep_review.py`, `test_orchestration_contracts.py` |
| Model catalog, provider compatibility, wizard and local policy | `test_model_policy.py` |
| Recovery, file permissions, diagnostic read-only behavior | `test_session_store.py`, `test_doctor.py` |
| MCP contracts and complete review flows | `test_server.py` |
| Platform launchers, environment filtering, client rules and packaging metadata | `test_install_artifacts.py`, `test_config.py` |

The test suite must run without network access, credentials, or external
services. Use temporary paths for mutable configuration and session stores.
An autouse fixture isolates the supported environment settings from the user's
configuration. Keep that isolation when adding tests. The real STDIO startup
test blocks outbound sockets and checks that no update cache is created;
preserve the disabled FastMCP banner because it otherwise checks PyPI.
The wheel build and initial dependency installation may need cached packages
or network access.

CI checks Python 3.12 and 3.13 on Linux and Windows, builds and installs the
wheel, and runs its doctor outside the checkout. Dependency auditing is a
separate workflow with scheduled and manual runs. A green test suite does not
prove a successful dependency audit, actual host model execution, or every
desktop client's connection. Report the checks you actually ran.

## Documentation and pull requests

- Keep examples free of personal paths, internal URLs, credentials, issue data,
  and source payloads. Use absolute placeholders for client paths.
- Update `docs/clients/` and `client-rules/` when host behavior changes. Keep
  orchestration mechanics in the canonical rules and local requirements in
  workspace/project instructions.
- Preserve existing instructions when describing installation. Explain which
  settings need a server restart and how to verify the loaded result.
- Describe the defect or motivation, behavior change, compatibility effects,
  and exact verification results in the pull request. Separate unavailable
  platform, provider, network, and runtime evidence.
- Check the staged diff and generated artifacts before committing. Follow the
  [security policy](SECURITY.md) for private vulnerability reports.
