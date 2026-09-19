# Remove Local Model Runtime and Make QA Router Deterministic

**Status:** Approved in chat on 2026-09-19; implementation pending plan review.

## Goal

Remove every Qwen, LM Studio, MLX, and local-generation dependency from QA Router and turn it into a deterministic MCP service that exposes QA review profiles, read-only review contracts, and content-free task metrics. The primary Codex remains responsible for evidence gathering, analysis, verification, final QA judgement, and all external-system actions.

## Context

The current server is a local drafting runtime. It validates sanitized input, calls `qwen/qwen3.5-9b` through LM Studio, validates generated drafts, maintains generation quality gates, and records model-specific telemetry. That runtime is not used in the actual QA workflow.

The required workflow is instead:

```text
authoritative sources + CodeGraph
        -> primary Codex
        -> deterministic QA Router profile/route metadata
        -> primary Codex analysis and verification
        -> optional host-owned qa_deep escalation
        -> final QA result and authorized external actions
```

## Design decision

QA Router will not invoke, configure, load, tokenize, or communicate with any model. It will not accept an Evidence Packet for generation. It will expose one deterministic review-route tool:

```python
prepare_review_route(agent_profile: ReviewAgent) -> ReviewRoute
```

`ReviewRoute` contains:

- the selected profile;
- the profile focus;
- the required output sections;
- read-only and decision-ownership constraints;
- escalation signals for the host agent.

The existing seven `ReviewAgent` values remain the source of truth. A host may call the tool once per profile for a focused pass; this does not create autonomous threads or background agents.

The route contract requires these output sections for the host's final review draft:

- `Scope`;
- `Checklist`;
- `Candidate Coverage Gaps`;
- `Positive Observations`;
- `Unverified`.

The router does not claim that a checklist item is a confirmed finding and does not determine severity, priority, root cause, release readiness, or merge readiness.

## MCP surface after the change

Keep:

- `prepare_review_route`;
- `record_qa_task_outcome`;
- `get_metrics_report`.

Remove all model-backed drafting tools and model-feedback tools:

- `draft_test_cases`;
- `draft_review_checklist`;
- `summarize_logs`;
- `draft_automation_skeleton`;
- `translate_text`;
- `rewrite_text`;
- `explain_short`;
- `summarize_text`;
- `record_canary_feedback`.

`record_qa_task_outcome` must not contain Qwen-specific fields. It continues to record task type, outcome, source-MCP and CodeGraph call counts, findings counters, repeated-source-read counters, and optional host-owned deep-analysis fields.

`get_metrics_report` must report only those content-free QA-task metrics. Generation events, model names, token counts, model-loading latency, canary state, shadow drafts, and repair metrics are removed.

## Code and dependency changes

- Replace the generation-oriented `RouterService` with deterministic route construction and task-outcome recording.
- Move the seven profile descriptions into a focused review-profile module; do not keep generation prompt or local-model instructions.
- Simplify settings to metrics path, retention, and event-count configuration.
- Remove the LM Studio backend, generation contracts, generation validation, local quality gate, and model-specific event/report code.
- Remove `lmstudio` and `httpx` runtime dependencies and refresh `uv.lock`.
- Remove the LM Studio/llmster launchd artifact and make the launcher start only the MCP server, or remove it if no client uses it.
- Delete opt-in live LM Studio tests and model/backend tests; replace them with route-contract and MCP-surface tests.
- Update README, routing policy, client rules, contributor documentation, and diagrams so no operational instruction mentions Qwen, LM Studio, MLX, local delegation, or canary draft feedback.
- Remove obsolete historical design/plan artifacts whose only purpose is the retired local-model architecture.

## Safety and ownership

- The router performs no external writes and no model calls.
- The primary Codex remains the only component that retrieves authoritative project evidence and decides whether a finding is confirmed.
- `qa_deep` remains optional, read-only, host-owned, and limited to the existing Sol/high escalation boundary.
- Task metrics remain content-free and must not store evidence, prompts, drafts, issue keys, paths, or logs.
- Invalid profiles and malformed metric inputs fail closed with typed validation errors.

## Acceptance criteria

1. Repository source, docs, scripts, launch artifacts, lockfile, and tests contain no operational Qwen, LM Studio, MLX, llmster, local-model, or local-generation references.
2. The MCP server starts without `lmstudio`, `httpx`, an LM Studio process, a model, or a loopback HTTP endpoint.
3. `prepare_review_route` returns the correct deterministic route for all seven profiles.
4. The MCP tool list contains only the deterministic route and content-free task-metrics tools.
5. Task metrics accept and report host-owned QA counters without Qwen-specific fields.
6. Tests cover every profile, unknown-profile rejection, the exact MCP surface, metric validation, and a clean no-model startup path.
7. `pytest -q` and `ruff check .` pass after the migration.

## Non-goals

- Adding another local model or cloud model.
- Making QA Router spawn agents or threads.
- Moving Jira, GitLab, TestRail, Sentry, Grafana, OpenSearch, Slack, Confluence, or CodeGraph access into QA Router.
- Automating severity, release, merge, root-cause, or external-write decisions.
