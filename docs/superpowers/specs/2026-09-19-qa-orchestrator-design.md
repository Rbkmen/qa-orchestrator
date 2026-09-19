# QA Router Host-Owned Orchestrator

**Status:** Approved by user on 2026-09-19

## Goal

Add a deterministic, host-owned orchestration layer to QA Router. The orchestrator coordinates a three-model QA flow without becoming a model runtime, source-system client, autonomous agent factory, or decision authority.

The primary host agent remains responsible for authoritative evidence, tool calls, verification, final QA judgment, code changes, and every external write.

## Model policy

The orchestration contract exposes model assignments and reasoning levels to the host client. QA Router does not invoke these models itself.

| Stage | Model | Reasoning | Responsibility |
|---|---|---|---|
| Triage | `gpt-5.6-luna` | `max` | Classify the task, identify the likely QA profile, and list evidence gaps |
| Primary review | `gpt-5.6-terra` | `medium` | Analyze requirements, diff, callers, contracts, tests, and verification evidence |
| Deep escalation | `gpt-5.6-sol` | `high` | Optional read-only analysis for complex or high-risk cases |

The normal flow is `Luna/max → Terra/medium → optional Sol/high → Terra/medium synthesis`. `max` is intentionally limited to the Luna triage stage; Sol is not raised automatically above `high`.

No local model, provider process, model API credential, or hidden fallback is part of the server.

## Responsibility boundary

### Host agent owns

- retrieval from Jira, GitLab, TestRail, Sentry, Grafana, OpenSearch, Slack, Confluence, CodeGraph, and repositories;
- construction and sanitization of the Evidence Packet;
- launching the configured model stages and supplying their inputs;
- interpreting model outputs and separating confirmed findings from hypotheses;
- the decision to request the Sol escalation;
- severity, priority, root cause, release/readiness judgment, code changes, and external writes.

### QA Router owns

- deterministic workflow state and allowed transitions;
- the selected review profile and model policy metadata;
- read-only constraints and the next host action;
- content-free orchestration and QA-task metrics.

The router never receives prompts, evidence, source text, model output, logs, issue keys, paths, or credentials.

## Orchestration state machine

An orchestration session is an in-memory, content-free state machine. It has an opaque `run_id`, task type, current step, selected review profile when known, model policy, transition history represented only by step/status metadata, and an expiry time. It does not persist across a server restart; the host starts a new session if needed.

The default states are:

```text
created
  -> luna_triage
  -> terra_primary_review
  -> terra_synthesis
  -> awaiting_host_outcome
  -> completed | partial | blocked

terra_primary_review
  -> sol_deep_review
  -> terra_synthesis
```

The Sol branch is entered only when the host reports that deeper read-only analysis is justified. It is never selected from raw evidence by the router. Any stage may end as `partial` or `blocked`; the router does not retry a model or silently switch tiers.

## MCP contracts

Keep the existing deterministic route and metrics tools. Add three orchestration tools:

### `start_qa_orchestration`

Creates a session for a `QaTaskType` and returns:

- `run_id`;
- current step and next host action;
- the Luna model policy (`gpt-5.6-luna`, `max`);
- allowed review profiles;
- `read_only=true` and `host_owns_decisions=true`.

It accepts no Evidence Packet or free-form task content.

### `advance_qa_orchestration`

Advances one valid state transition using structured, content-free signals only:

- completed step;
- step status: `completed`, `partial`, or `blocked`;
- selected `ReviewAgent` after Luna triage;
- `needs_deep_analysis=true` to request the optional Sol branch;
- optional reason code from a fixed enum such as `evidence_gap`, `cross_repository`, `security_sensitive`, `payment_sensitive`, `root_cause`, or `high_blast_radius`.

It returns the next step, its assigned model/reasoning, the route profile, and the constraints for the host. Invalid ordering, unknown sessions, and incompatible signals fail closed.

### `get_qa_orchestration`

Returns the current content-free session state and next action. It never returns model prompts, outputs, evidence, or source references.

`record_qa_task_outcome` remains the single metrics write for a finished task. The host calls it once after the session reaches `awaiting_host_outcome`, `partial`, or `blocked`.

## Model-stage behavior

### Luna triage

The host gives Luna a minimal Evidence Packet. Luna may suggest one of the seven profiles and identify missing verification, but cannot confirm findings, set severity, decide readiness, or perform writes. The host validates the suggested profile before advancing the session.

### Terra primary review

Terra performs the main implementation-aware review using the selected profile. Its output is treated as candidate analysis. The host checks callers, contracts, tests, runtime evidence, and repository rules before accepting any finding.

### Sol deep review

Sol receives only the smallest relevant Evidence Packet plus the concrete question that justified escalation. It is read-only and may challenge Terra's assumptions, identify cross-repository impact, or expose high-risk gaps. The host, not Sol or the router, decides whether the result changes the final QA answer.

### Terra synthesis

The host invokes `gpt-5.6-terra` with `medium` reasoning for synthesis and validates the result in the primary client before delivery. It produces the standard QA result:

- Findings;
- Changes;
- Manual Test Plan;
- Open Questions / Could Not Verify.

Synthesis never authorizes a merge, release, severity, or external write by itself.

## Failure and recovery

- Unknown profile, unknown run, expired session, or illegal transition: typed error and no state change.
- Model timeout or unavailable host model: host records `partial` or `blocked`; no automatic fallback tier.
- Sol not required: host advances directly from Terra primary review to Terra synthesis.
- Server restart: active sessions are discarded; the host starts a fresh session without losing source data because the router never held it.
- Session limits and TTL prevent unbounded in-memory growth.

## Metrics

Extend content-free task metrics with optional orchestration counters:

- `orchestration_used`;
- `luna_calls`, `terra_calls`, `sol_calls`;
- `orchestration_steps_completed`;
- `orchestration_retries`.

These are non-negative counters supplied by the host. They do not contain model prompts, outputs, issue identifiers, source paths, or task text. The existing source-MCP, CodeGraph, findings, repeated-read, and deep-analysis measurements remain unchanged.

## Testing requirements

Tests must cover:

1. Luna → Terra → Terra synthesis without Sol;
2. Luna → Terra → Sol → Terra synthesis;
3. all seven profiles and their model assignments;
4. illegal transitions, expired sessions, unknown runs, and invalid signals;
5. no evidence or free-form content accepted by orchestration tools;
6. content-free orchestration metrics and the existing exact MCP surface plus the three new tools;
7. clean startup with no model process, model endpoint, or provider dependency.

## Non-goals

- Calling models from the QA Router process;
- retrieving Jira, GitLab, TestRail, monitoring, Slack, Confluence, or repository data from the router;
- spawning autonomous Codex/Claude/Cursor agents or threads;
- automatic severity, root-cause, release, merge, or external-write decisions;
- persistent conversation, evidence, model output, or QA memory.
