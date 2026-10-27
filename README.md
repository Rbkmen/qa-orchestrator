# QA Orchestrator

[![QA Orchestrator MCP server – quality and maintenance score on Glama](https://glama.ai/mcp/servers/Rbkmen/qa-orchestrator/badges/card.svg)](https://glama.ai/mcp/servers/Rbkmen/qa-orchestrator)

A Python/FastMCP toolkit for evidence-bound code and QA reviews: deterministic workflow, eleven specialist profiles, nine fixed bundles, structured findings, and optional execution and evaluation tools.

The **host** (Codex, Claude Code, or another integration) retrieves evidence, runs models, validates findings, and owns the final QA decision. The MCP server checks workflow and report contracts. Optional Python tools supervise actual execution; connecting the MCP server alone does not activate them.

## Quick start

Requires Python 3.12+, Git, [uv](https://docs.astral.sh/uv/), and an MCP host. Supports Windows, macOS, and Linux. Model access is configured in the host.

For installation without a checkout, replace `<commit-sha>` with the full immutable revision you intend to install:

```bash
codex mcp add qa-orchestrator -- uvx --from 'git+https://github.com/Rbkmen/qa-orchestrator.git@<commit-sha>' qa-orchestrator-mcp
```

Use the same revision for server and host instructions. Remote installs contain only published code; local upgrades require a checkout installation.

With no saved policy, `model_policy` is `host_default`: the host uses its own model and reasoning. Existing explicit policies remain active.

For a checkout installation:

```bash
git clone https://github.com/Rbkmen/qa-orchestrator.git
cd qa-orchestrator
uv sync --locked
uv run qa-orchestrator-doctor --json
```

Register `scripts/qa-orchestrator` on macOS/Linux or `.venv\Scripts\qa-orchestrator-mcp.exe` on Windows. The client starts the STDIO server on demand. See [Installation](docs/INSTALLATION.md), [Codex](docs/clients/codex.md), and [Claude Code](docs/clients/claude-code.md).

Reference the [canonical host rules](client-rules/generic/QA_ORCHESTRATOR_INSTRUCTIONS.md) from existing instructions. Verify the connection with `get_qa_orchestration_catalog`, `get_qa_orchestration_model_policy`, and `prepare_qa_orchestration(agent_profile="code_explorer")`; these calls create no session.

## Architecture and responsibilities

| Layer | Responsibility |
|---|---|
| MCP server | Checklists, stage/group validation, fixed escalation rules, bounded session state |
| Evidence utilities | Immutable Git source/diff hashes, citation coordinates, exact context packets |
| `HostReviewAdapter` | Async executor supervision, concurrency/time budgets, group barriers and attempts |
| `CodexProfileExecutor` | Installed CLI execution, strict output schema, report validation and reported usage |
| Benchmark tools | Snapshot identity and scoring of explicitly adjudicated runs |

The host owns source retrieval, defect confirmation, severity, final decisions, code edits, and external writes. Jira, GitLab, CI, TestRail, Sentry, and other sources come through the host's tools. Initialization instructions are advisory; `read_only=true` and `host_owns_decisions=true` describe review ownership, while session tools mutate local state.

## Review workflow

```text
Pinned evidence → triage → primary review groups → optional deep review
                → synthesis → awaiting_host_outcome → host final outcome
```

1. Build the evidence input; use `ReviewContext` for an auditable bound review.
2. Call `start_qa_orchestration`, then complete triage through `advance_qa_orchestration` with one `selected_profile` or `selected_bundle`.
3. Run the returned `execution_groups`. Profiles in one group may run concurrently; wait for all, then record the ordered `completed_profiles` and reports in one transition. Transition calls are sequential.
4. Supply evidence-based `risk_signals` on the last primary-review transition. Perform any required deep review, then validate and synthesize the result.
5. Call `finish_qa_orchestration` with `completed`, `partial`, or `blocked`. A completed bound review requires `final_review_report`. Record early stops through `advance_qa_orchestration` first.

Use the session returned by each transition. Read/recover with `get_qa_orchestration` or `list_qa_orchestrations`. Revision checks and request keys protect against stale submissions and lost-response retries.

`completed` records workflow completion; it is not a CI pass or release approval. For a clean **single-profile** route, `skip_synthesis=true` skips the separate synthesis stage, but still requires host validation and a final report. Findings, material unresolved questions, coverage gaps, unverified notes, or risk signals prevent the shortcut. Bundles retain synthesis.

### Deep-review rules

| Trigger | Required signals |
|---|---|
| High-risk uncertainty | `high_risk_domain` and `evidence_uncertain` |
| Multiple complexity signals | At least two of `cross_system_scope`, `multiple_plausible_causes`, `non_reproducible`, `high_blast_radius` |
| Critical evidence conflict | `evidence_conflict` and at least one of `high_risk_domain`, `cross_system_scope`, `high_blast_radius` |

Enabled signals require rationale and evidence refs. Missing CI, Stage, or device execution alone does not require escalation; uncertainty must concern a material question that scoped review could resolve.

**Example:** ordinary QA of a small Ruby guard change checks requirements, the changed branch, callers, and assertions. The host can select `ruby_reviewer`, execute its checklist, submit its report and risk signals, synthesize, then finalize. See the [complete MCP tool contract](docs/ORCHESTRATION_POLICY.md#mcp-tools) for payloads.

## Profiles and bundles

Use one profile for a narrow, low-risk concern; use a bundle for broad or cross-concern work. File count alone does not define scope.

At triage, set `implementation_review=true` when implementation behavior is in scope. A lone `pr_test_analyzer` or `code_explorer` cannot report implementation findings: choose an implementation profile or bundle, or explicitly set `acknowledge_limited_profile=true` to proceed with that limitation. Older callers may omit the intent; they receive an advisory `route_warning` instead. The host determines intent and remains responsible for choosing an adequate route.

| Profile | Focus |
|---|---|
| `code_explorer` | Faraday — Evidence Investigator: paths, callers, dependencies and evidence map |
| `code_reviewer` | Changed-code failure modes and contract breaks |
| `pr_test_analyzer` | Test coverage and meaningful assertion gaps |
| `security_reviewer` | Authentication, authorization, input validation and sensitive data |
| `silent_failure_hunter` | Swallowed errors, fallbacks, timeout/rollback and error propagation |
| `typescript_reviewer` | Types, narrowing, casts, async and error contracts |
| `react_reviewer` | State, effects, rendering, accessibility and visible behavior |
| `ruby_reviewer` | Ruby semantics, exceptions, persistence and relevant jobs |
| `python_reviewer` | Validation, serialization, cancellation, cleanup and MCP contracts |
| `mobile_reviewer` | React Native/native lifecycle, navigation and platform contracts |
| `omission_auditor` | Final evidence-backed search for distinct failure modes missed by earlier profiles |

Test Analyzer returns coverage gaps; Evidence Investigator returns an evidence map and unresolved links. Both are prohibited from returning implementation findings, enforced by server validation and the Codex output schema.

In the schedules below, `+` means one concurrent-capable group; `→` means a barrier between groups.

| Bundle | Execution groups |
|---|---|
| `ordinary_mr` | `code_explorer` → `code_reviewer` + `pr_test_analyzer` → `omission_auditor` |
| `widget` | `code_explorer` → `react_reviewer` + `typescript_reviewer` + `pr_test_analyzer` → `omission_auditor` |
| `widget_js` | `code_explorer` → `code_reviewer` + `react_reviewer` + `pr_test_analyzer` → `omission_auditor` |
| `ruby_backend` | `code_explorer` → `ruby_reviewer` + `pr_test_analyzer` → `omission_auditor` |
| `python_backend` | `code_explorer` + `python_reviewer` + `pr_test_analyzer` → `omission_auditor` |
| `mobile` | `code_explorer` → `mobile_reviewer` + `pr_test_analyzer` → `omission_auditor` |
| `security` | `code_explorer` → `security_reviewer` + `silent_failure_hunter` → `omission_auditor` |
| `autotest` | `code_reviewer` + `typescript_reviewer` + `pr_test_analyzer` → `omission_auditor` |
| `requirements` | `code_explorer` → `code_reviewer` → `omission_auditor` |

The Python bundle starts its three primary profiles together, then runs `omission_auditor` after their reports are available. Required context must be supplied upfront. Reports from previous groups are available only to subsequent groups. The installed catalog is authoritative; see the [bundle and profile catalog](docs/ORCHESTRATION_POLICY.md#fixed-bundles-and-profile-names).

## Evidence and context

A bound `ReviewContext` pins repository identity, full base/head commits, diff and packet SHA-256, changed paths, and evidence IDs. Discovery can also bind its exact candidate set and a diff-ordered hunk inventory (up to 500 hunks) to the review. Partial or unavailable hunk inventory blocks strict completion. Profiles attest inspected hunk indexes; the report labels this as an attestation, not proof of reasoning. Changed-path coverage still accounts for binary and metadata-only changes that have no text hunks. Optional registry metadata adds source hashes, coordinates, text availability and external locators. Binary and non-UTF-8 source stays hash-bound but cannot support source coordinates. Bound profiles submit structured reports; finding coordinates must be supported by cited, text-available head-source records when a registry is supplied.

Local utilities verify immutable Git bytes, hashes and coordinates. External requirements/runtime evidence remains unverified by the Git checker. The MCP server validates identifiers and citations; it does not read the raw packet.

Exact context packing retains original UTF-8 fragments without summarizing or truncating them. The bound diff and supplied requirements remain mandatory; changed-source excerpts can be omitted or made profile-optional when the diff covers the change. Optional omissions are declared, and reports cannot cite omitted refs. A precise finding location still requires a cited head-source excerpt for that path. Mandatory overflow fails before execution. The Codex executor keeps a 1,000,000-byte context budget and a 2 MB serialized packet limit, while capping the complete CLI prompt at 1,000,000 UTF-8 bytes to stay below Codex CLI's 1,048,576-character input limit. These are byte/character limits, not token limits. Source caching is bounded and in memory; Python AST chunks preserve source coordinates.

See [Evidence Registry](docs/EVIDENCE_REGISTRY.md) and [context packing](docs/REVIEW_UPGRADES.md#7-exact-context-packing).

## Findings and final decisions

Reports separate candidates, coverage gaps, unverified claims and evidence maps. The host assigns every candidate one verdict: `confirmed`, `rejected`, `duplicate`, or `unverified`, with rationale and evidence. Final findings retain profile/candidate lineage; confirmation requires severity, impact, affected area and source locations. Finalization checks the complete decision ledger.

`failure_condition` and `flip_condition` describe the failure trigger and evidence that would refute it. New candidates also state `expected_behavior` from a requirement/contract and `actual_behavior` predicted from source under that trigger; predicted behavior is not runtime evidence. Confirmed findings name the affected consumer or contract in `affected_area` when supported. Conditioned candidates require a context/candidate-bound counterevidence check; confirmation or duplicate linkage requires `not_refuted`. The host performs that inspection. Legacy candidates may omit these fields.

Explicit scoped `ReviewRuleset` rules can raise concerns or guard against known false-positive patterns. They bind reports through a digest and guide inspection; they do not automatically suppress findings or learn from reviews. See [rules and counterevidence](docs/REVIEW_UPGRADES.md).

## Host execution

`HostReviewAdapter` supervises an injected async `execute_profile(job)` for a bound session with triage complete. It enforces group barriers, concurrency/time limits, lease renewal and cancellation. It stops at deep review or synthesis; later stages and final decisions remain with the host.

Rendered final reports include review boundaries: recorded outcome when supplied, reported profiles, retrieved/incomplete sources and reported limitations. For partial or blocked outcomes they also list known completion gaps; these are evidence of gaps, not necessarily the host's recorded reason. Standalone `qa-orch report render` accepts `--outcome` when the session outcome is known; otherwise it displays that outcome was not provided.

Timeouts and explicit `RetryableProfileError` can be retried. Permanent failures are not replayed automatically. A failed group is never recorded as complete; the run becomes `partial`, retaining successful unsaved reports.

`CodexProfileExecutor` uses an authenticated installed CLI, a fresh temporary workspace, read-only sandbox, no inherited user configuration, and a strict JSON schema. Invalid output or detected forbidden tool actions fail the call; timeout/cancellation terminates its process tree. In `host_default`, it requires an explicit model. Other runtimes use a custom executor.

Optional checkpoints reuse validated reports only when context, rules, checklist, model policy, previous-group reports and executor/instruction identity match. Reuse is reported separately; disable it for independent benchmark repetitions. See [Host Adapter](docs/HOST_ADAPTER.md) and [execution tools](docs/REVIEW_UPGRADES.md).

## Performance and evaluation

Narrow routing, concurrent groups, the clean-profile synthesis shortcut, checkpoints and bounded context selection can avoid unnecessary work. Execution speed and latency preferences remain controlled by the user's host/provider settings; improvement requires measurement.

`execution_attempts` records fresh successful, failed and cancelled calls, queue wait, duration and available phase/token observations. Missing usage remains unknown, never zero. Checkpoint reuse is separate; `model_seconds` includes the CLI/process interval rather than provider-only inference. No automatic price estimate is produced.

Benchmark tools score pinned runs using explicit adjudications; they do not launch models or decide matches. The explicit-threshold quality gate is experimental. A named policy pins the dataset kind and digest, case/gold coverage and quality thresholds; `blocked` means its evidence is incomplete or mismatched, while `failed` means measured quality misses a threshold. No approved production policy ships in this repository.

Speed comparisons report p95 and sample counts, keep missing queue measurements unknown, and assess recorded run order. Interleaving must alternate configurations; randomized order is reported as host-declared, not independently verified. `compare` is exploratory without `--quality-policy`; a gated candidate must meet the same pinned policy as its baseline and the policy's minimum repetition count. These tools do not prove adjudicator correctness or fresh independent model execution. See [Benchmark](docs/BENCHMARK.md), [Profile Evaluation](docs/PROFILE_EVALUATION.md), and the [audited ReviewBench baseline](docs/REVIEWBENCH_BASELINE.md).

## MCP tools

| Tool | Purpose |
|---|---|
| `get_qa_orchestration_catalog` | Profiles, bundles and schedules |
| `prepare_qa_orchestration` | One profile checklist |
| `prepare_qa_orchestration_bundle` | Bundle checklists and groups |
| `get_qa_orchestration_model_policy` | Running server's policy snapshot |
| `start_qa_orchestration` | Create a session |
| `advance_qa_orchestration` | Validate and record a transition/reports |
| `renew_qa_orchestration` | Extend an active lease |
| `get_qa_orchestration` | Read a retained session |
| `list_qa_orchestrations` | Discover retained session IDs |
| `finish_qa_orchestration` | Finalize host outcome and report |
| `delete_qa_orchestration` | Irreversibly discard local session state |

Reads do not extend TTL. Deletion neither finalizes a review nor cancels host execution.

## CLI tools

| Command | Purpose |
|---|---|
| `qa-orch setup` / `setup --host-default` | Configure per-stage models or return to host settings |
| `qa-orch config show` / `reload` | Inspect/validate the saved policy |
| `qa-orch evidence discover` | Suggest pinned declarations, callers/callees, direct importers, contracts, tests, and bounded test-audit signals; reports traced relations and bounded omissions; `--bound-context` binds the exact candidate set and diff hunk inventory |
| `qa-orch evidence build` / `verify` | Register/verify source and diff |
| `qa-orch evidence check-links` | Explicit, host-allowlisted reachability check for external evidence URLs; does not verify external content hashes |
| `qa-orch evidence pack` / `chunks` | Exact context packets/source chunks |
| `qa-orch repo profile` / `audit-tests` | Profile tracked project/test/CI metadata and report source-anchored test-suite review candidates |
| `qa-orch rules` | Bind explicit review rules |
| `qa-orch benchmark import-reviewbench` / `import-reviewbench-set` / `diff-budget` / `score` / `reliability` / `compare` / `quality-gate` | Import references, score explicit adjudications, compare speed, and enforce declared quality thresholds |
| `qa-orchestrator-doctor --json` | Read-only local diagnostics |

`qa-orchestrator` aliases `qa-orch`. Running either without a subcommand starts the MCP server. These tools do not provide a one-command MR review and publication flow.

Repository profiling and test-suite audit operate on committed Git objects and do not change the repository; `profile` writes a local cache unless `--no-cache` is set. Candidate signals are not confirmed findings; see [Repository profile and test audit](docs/REPOSITORY_AUDIT.md) for limits and examples.

## Configuration

`host_default` uses the host's model/reasoning. Optional `configured` policy sets provider, model and effort per stage. Setup validates local metadata, not account access or live model capabilities; no provider credentials are stored.

| Variable | Default |
|---|---|
| `QA_ORCHESTRATOR_DATA_DIR` | `~/.qa-orchestrator` |
| `QA_ORCHESTRATOR_MODEL_POLICY_PATH` | `<data-dir>/model-policy.json` |
| `QA_ORCHESTRATOR_ORCHESTRATION_TTL_SECONDS` | `1800` |
| `QA_ORCHESTRATOR_ORCHESTRATION_MAX_SESSIONS` | `100` |
| `QA_ORCHESTRATOR_SESSION_STORE_PATH` | Unset; opt-in SQLite recovery |

Pass overrides through the MCP client's environment. Restart the server connection after code/policy changes: CLI `reload` does not update that process. Inspect the loaded policy with `get_qa_orchestration_model_policy`; neither configuration nor session metadata proves model execution.

## Storage and trust boundaries

Session state is bounded and in memory by default. Optional SQLite restores non-expired unfinished sessions and finalized bound reviews with structured reports. Finalized legacy sessions without artifacts are removed. Checkpoints use a separate opt-in store; use one server process per session-store file.

Stored reports can contain source-derived findings and descriptions. Raw packets, prompts, transcripts and benchmark artifacts are managed separately by the host. Reports are limited to 64 KiB each and 256 KiB combined per session. Normal MCP calls need no network; installation, evidence tools and model execution may need it.

Validation proves structure, identity and reported sequence, not source completeness, defect truth or runtime success. The CLI read-only sandbox restricts writes, not all reads; strict read isolation needs an OS/container boundary. Forbidden-action detection cannot prove an action never started. No autonomous external writes or learned persistent QA memory are added.

## Development and documentation

```bash
uv run pytest -q
uv run ruff check .
uv run qa-orchestrator-doctor --json
git diff --check
```

Implementation: `src/qa_orchestrator/`. Tests cover contracts, workflow, storage, evidence and execution supervision; real-model quality and target application/device checks are separate.

See [CONTRIBUTING.md](CONTRIBUTING.md), [Orchestration Policy](docs/ORCHESTRATION_POLICY.md), [Review Upgrades](docs/REVIEW_UPGRADES.md), and [SECURITY.md](SECURITY.md). Licensed under [MIT](LICENSE).
