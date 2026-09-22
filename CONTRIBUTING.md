# Contributing to QA Orchestrator

QA Orchestrator must remain a small deterministic service: fixed routing, host-owned orchestration, and content-free metrics.

## Development

Requirements:

- Python 3.12+;
- [`uv`](https://docs.astral.sh/uv/);
- an MCP client for manual STDIO verification.

```bash
git clone https://github.com/Rbkmen/qa-orchestrator.git
cd qa-orchestrator
uv sync
uv run pytest -q
uv run ruff check .
```

The full test suite must not require network access, credentials, or a separate external service.

## Architecture boundaries

- The primary host obtains sources, builds the Evidence Packet, runs model stages, and makes the final QA decision.
- `prepare_review_route` returns only static metadata for the selected profile.
- `start_qa_orchestration`, `advance_qa_orchestration`, and `get_qa_orchestration` manage content-free state only.
- The orchestrator does not call models, create threads or agents, write user-requested files, or perform writes to external systems.
- The model policy is fixed: Luna/max for triage, Terra/medium for primary review and synthesis, and optional Sol/high for read-only deep analysis.
- Metrics contain only the task type, outcome, and aggregate counters; task content is prohibited.
- Do not add persistent QA memory, a source cache, a learning layer, or hidden external calls.

## Changing the MCP contract

The public surface must remain limited to six tools:

1. `prepare_review_route` — profile metadata;
2. `start_qa_orchestration` — create a session;
3. `advance_qa_orchestration` — validated transition;
4. `get_qa_orchestration` — state and next action;
5. `record_qa_task_outcome` — content-free counters;
6. `get_metrics_report` — aggregate read-only report.

When changing the contract, update `contracts.py`, `orchestration.py`, `service.py`, `server.py`, tests, the README, the routing policy, and client rules. For every new branch, add checks for input validation, illegal transitions, expiry/limits, and the absence of task content.

## Testing

```bash
uv run pytest -q
uv run ruff check .
git diff --check
```

Tests must cover observable behavior: the exact MCP tool set, every profile, the model policy, state transitions, read-only flags, invalid counters, retention, and the absence of task content in JSONL.

## Documentation and client rules

Update [docs/ORCHESTRATION_POLICY.md](docs/ORCHESTRATION_POLICY.md) when responsibility boundaries change. When host behavior changes, update the guides in `docs/clients/` and the templates in `client-rules/`. Do not add internal URLs, credentials, issue data, local absolute paths, or source payloads to examples.

## Commits and review

- Keep changes focused.
- Describe the motivation, behavior change, and verification.
- Call out anything that could not be verified separately.
- Before review, run `pytest`, Ruff, `git diff --check`, the exact tool-surface check, and the generated/local-artifact check.
