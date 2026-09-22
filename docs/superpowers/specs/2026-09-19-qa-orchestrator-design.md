# QA Orchestrator Host-Owned Design

**Status:** Approved by user on 2026-09-19

## Goal

Add a deterministic, host-owned orchestration layer to QA Orchestrator. The orchestrator coordinates a three-model QA flow and a fixed catalog of specialist review profiles without becoming a model runtime, source-system client, autonomous agent factory, or decision authority.

The implementation must allow one task to run a deterministic bundle of specialist profiles together. The bundle is selected during Luna triage, executed by Terra in the host, and synthesized by Terra after host verification. This adds coordinated specialist coverage without adding another model provider or another autonomous agent role.

The primary host agent remains responsible for authoritative evidence, tool calls, verification, final QA judgment, code changes, and every external write.

## Model policy

The orchestration contract exposes model assignments and reasoning levels to the host client. QA Orchestrator does not invoke these models itself.

| Stage | Model | Reasoning | Responsibility |
|---|---|---|---|
| Triage | `gpt-5.6-luna` | `max` | Classify the task, identify the likely profile or bundle, and list evidence gaps |
| Primary review | `gpt-5.6-terra` | `medium` | Analyze requirements, diff, callers, contracts, tests, and verification evidence |
| Deep escalation | `gpt-5.6-sol` | `high` | Optional read-only analysis for complex or high-risk cases |

The normal flow is `Luna/max → Terra/medium → optional Sol/high → Terra/medium synthesis`. `max` is intentionally limited to the Luna triage stage; Sol is not raised automatically above `high`.

No model runtime, provider process, model API credential, or hidden fallback is part of the server.

## Named specialist profiles and review bundles

The specialist profiles are deterministic review roles, not separate model runtimes. Their technical identifiers remain stable for MCP clients; the host may display the following names:

| Technical profile | User-facing name | Responsibility |
|---|---|---|
| `code_explorer` | `Faraday — Evidence Investigator` | Map callers, dependencies, contracts, and evidence gaps |
| `code_reviewer` | `Code Reviewer` | Review changed behavior, failure paths, and regression risk |
| `pr_test_analyzer` | `Test Analyzer` | Check test intent, branches, assertions, and missing protection |
| `security_reviewer` | `Security Reviewer` | Check auth, validation, secrets, payment, and access boundaries |
| `silent_failure_hunter` | `Silent Failure Hunter` | Find swallowed errors, false success, fallback, timeout, and observability gaps |
| `typescript_reviewer` | `TypeScript Reviewer` | Check types, async contracts, serialization, and unsafe casts |
| `react_reviewer` | `React Reviewer` | Check state, effects, rendering, accessibility, and user-visible behavior |

`Faraday` is an internal display name for the `code_explorer` profile. It is not an external Faraday service, model, provider, or credential.

Luna selects one fixed bundle or one single profile. The initial fixed bundles are:

| Bundle | Ordered profiles |
|---|---|
| `ordinary_mr` | `code_explorer`, `code_reviewer`, `pr_test_analyzer` |
| `widget` | `code_explorer`, `react_reviewer`, `typescript_reviewer`, `pr_test_analyzer` |
| `security` | `code_explorer`, `security_reviewer`, `silent_failure_hunter` |
| `autotest` | `code_reviewer`, `pr_test_analyzer`, `typescript_reviewer` |
| `requirements` | `code_explorer`, `code_reviewer` |

The host runs the selected profiles in order during the Terra primary-review stage. The orchestrator exposes the ordered profile list as content-free metadata; it does not run or prompt the profiles. A single-profile selection remains supported for compatibility and is represented as a one-item profile list.

The public contract adds a `ReviewBundle` enum and fixed `REVIEW_BUNDLES` mapping. A session exposes `selected_bundle`, compatibility field `selected_profile`, and ordered `review_profiles`. After Luna completes, exactly one of `selected_bundle` or `selected_profile` must be supplied. A bundle expands to its immutable profile order; a single profile expands to a one-item list. The host cannot submit an arbitrary list or reorder a bundle.

## Responsibility boundary

### Host agent owns

- retrieval from Jira, GitLab, TestRail, Sentry, Grafana, OpenSearch, Slack, Confluence, CodeGraph, and repositories;
- construction and sanitization of the Evidence Packet;
- conversion of the evidence state into boolean, content-free deep-review signals;
- launching the configured model stages and supplying their inputs;
- interpreting model outputs and separating confirmed findings from hypotheses;
- validating the orchestrator's deterministic escalation assessment;
- severity, priority, root cause, release/readiness judgment, code changes, and external writes.

### QA Orchestrator owns

- deterministic workflow state and allowed transitions;
- the selected review bundle or single profile, ordered profile metadata, and model policy metadata;
- deterministic evaluation of host-supplied deep-review signals;
- read-only constraints and the next host action;
- content-free orchestration and QA-task metrics.

The orchestrator never receives prompts, evidence, source text, model output, logs, issue keys, paths, or credentials.

## Orchestration state machine

An orchestration session is an in-memory, content-free state machine. It has an opaque `run_id`, task type, current step, selected review bundle or profile when known, ordered profile metadata, model policy, transition history represented only by step/status metadata, and an expiry time. It does not persist across a server restart; the host starts a new session if needed.

When a bundle is selected, the session contains only the fixed bundle identifier and its ordered specialist profile list. It does not contain profile prompts, evidence, outputs, source references, or arbitrary display text.

The default states are:

```text
created
  -> luna_triage
  -> terra_primary_review(profile[1..N])
  -> terra_synthesis
  -> awaiting_host_outcome
  -> completed | partial | blocked

terra_primary_review(profile[i])
  -> terra_primary_review(profile[i+1])
  -> sol_deep_review          (only after profile[N])
  -> terra_synthesis          (only after profile[N])
```

The Sol branch is entered when the host supplies structured signals that match one of the fixed deep-review rules. It is never selected from raw evidence by the orchestrator. Any stage may end as `partial` or `blocked`; the orchestrator does not retry a model or silently switch tiers.

## MCP contracts

Keep the existing deterministic route and metrics tools. Add three orchestration tools:

`prepare_review_route(agent_profile)` remains the single-profile route entrypoint and returns the stable technical profile, its user-facing display name, focus, required sections, constraints, escalation signals, and read-only ownership flags. Bundle execution reuses this route for each profile in the ordered list; it does not add a second model or an external agent call.

### `start_qa_orchestration`

Creates a session for a `QaTaskType` and returns:

- `run_id`;
- current step and next host action;
- the Luna model policy (`gpt-5.6-luna`, `max`);
- allowed review profiles and fixed review bundles;
- `read_only=true` and `host_owns_decisions=true`.

It accepts no Evidence Packet or free-form task content.

### `advance_qa_orchestration`

Advances one valid state transition using structured, content-free signals only:

- completed step;
- step status: `completed`, `partial`, or `blocked`;
- exactly one selected fixed bundle or one `ReviewAgent` after Luna triage;
- `completed_profile` after each completed Terra primary profile; the orchestrator requires the current profile and the fixed bundle order;
- `risk_signals` in the transition that completes the final Terra primary profile; the orchestrator applies the fixed deep-review rules and returns a content-free assessment;
- `needs_deep_analysis=true` plus one fixed reason code remains available for compatibility;
- optional reason code from a fixed enum such as `evidence_gap`, `cross_repository`, `security_sensitive`, `payment_sensitive`, `root_cause`, or `high_blast_radius` on the compatibility path.

It returns the next step, its assigned model/reasoning, the selected bundle, ordered profile list, current profile, completed profiles, `deep_assessment` when risk signals were supplied, and the constraints for the host. Invalid ordering, skipped profiles, early Sol/synthesis, unknown sessions, incompatible bundle/profile signals, and arbitrary profile names fail closed.

### `get_qa_orchestration`

Returns the current content-free session state and next action. It never returns model prompts, outputs, evidence, or source references.

`record_qa_task_outcome` remains the single metrics write for a finished task. For an orchestrated task the host calls it once with the same opaque `run_id` after the session reaches `awaiting_host_outcome`, `partial`, or `blocked`; Orchestrator validates and finalizes that session without storing the identifier in metrics. Repeating the same `run_id` and outcome is idempotent.

## Model-stage behavior

### Luna triage

The host gives Luna a minimal Evidence Packet. Luna may suggest one fixed bundle or one of the seven profiles and identify missing verification, but cannot confirm findings, set severity, decide readiness, or perform writes. The host validates the suggestion against the fixed catalog before advancing the session.

### Terra primary review

Terra performs the main implementation-aware review using the selected profile or ordered bundle. For a bundle, Terra runs each profile in order and returns candidate analysis for each role. The host calls `advance_qa_orchestration` with that role's `completed_profile` before the orchestrator exposes the next role; synthesis and Sol are unavailable until the final profile is complete. The host checks callers, contracts, tests, runtime evidence, and repository rules before accepting any finding.

The host may report user-facing status at profile boundaries, for example: `Terra / Medium → Faraday — Evidence Investigator`, then `Terra / Medium → Code Reviewer`. The orchestrator does not emit conversational progress messages itself.

### Sol deep review

Sol receives only the smallest relevant Evidence Packet plus the concrete question that justified escalation. It is read-only and may challenge Terra's assumptions, identify cross-repository impact, or expose high-risk gaps. The orchestrator selects this branch from boolean signals using these rules: high risk plus uncertainty; at least two complexity signals; or a high-impact evidence conflict. The host, not Sol or the orchestrator, decides whether the result changes the final QA answer.

### Terra synthesis

The host invokes `gpt-5.6-terra` with `medium` reasoning for synthesis and validates the result in the primary client before delivery. It produces the standard QA result:

- Findings;
- Changes;
- Manual Test Plan;
- Open Questions / Could Not Verify.

Synthesis never authorizes a merge, release, severity, or external write by itself.

## Failure and recovery

- Unknown profile or bundle, incompatible bundle/profile selection, altered or skipped profile order, early synthesis/Sol, unknown run, expired session, or illegal transition: typed error and no state change.
- Model timeout or unavailable host model: host records `partial` or `blocked`; no automatic fallback tier.
- Sol not required: host advances directly from Terra primary review to Terra synthesis.
- Server restart: active sessions are discarded; the host starts a fresh session without losing source data because the orchestrator never held it.
- Session limits and TTL prevent unbounded in-memory growth.

## Metrics

Extend content-free task metrics with optional orchestration counters:

- `orchestration_used`;
- `luna_calls`, `terra_calls`, `sol_calls`;
- `orchestration_steps_completed`;
- `orchestration_retries`.
- optional per-stage token counters for Luna, Terra primary review, Sol, and Terra synthesis;
- optional Evidence Packet, merge-request, and repository scope counters;
- optional Sol-value counters for identified, newly confirmed, and rejected findings.

The token fields are `luna_input_tokens`, `luna_output_tokens`, `terra_primary_input_tokens`, `terra_primary_output_tokens`, `deep_input_tokens`, `deep_output_tokens`, `terra_synthesis_input_tokens`, and `terra_synthesis_output_tokens`. Scope fields are `evidence_packet_tokens`, `merge_requests_count`, and `repositories_count`.
- `deep_escalation_recommended` and fixed `deep_escalation_reason_codes`.

These are non-negative counters supplied by the host. They do not contain model prompts, outputs, issue identifiers, source paths, or task text. The existing source-MCP, CodeGraph, findings, repeated-read, and deep-analysis measurements remain unchanged.

For an orchestrated Sol branch, `deep_model=gpt-5.6-sol` and `deep_reasoning=high` are required to preserve the fixed model policy. Deep duration and token measurements remain optional.

If profile-level metrics are added, they must remain aggregate non-negative counters only; profile names, prompts, evidence, findings, and model outputs must not be stored.

## Testing requirements

Tests must cover:

1. Luna → Terra → Terra synthesis without Sol;
2. Luna → Terra → Sol → Terra synthesis;
3. every fixed bundle, its exact profile order, and the single-profile compatibility path;
4. all seven profiles, their display names, and their model assignments;
5. illegal transitions, expired sessions, unknown runs, and invalid signals;
6. deterministic deep-review rules for high risk plus uncertainty, two complexity signals, critical evidence conflict, and non-escalating low-risk signals;
7. no evidence or free-form content accepted by orchestration tools;
8. content-free orchestration metrics and the existing exact MCP surface plus the three new tools;
9. clean startup with no model process, model endpoint, external Faraday/Qodo/Devin/Jules dependency, or provider dependency.

## Non-goals

- Calling models from the QA Orchestrator process;
- retrieving Jira, GitLab, TestRail, monitoring, Slack, Confluence, or repository data from the orchestrator;
- spawning autonomous Codex/Claude/Cursor agents or threads;
- integrating external Faraday, Qodo, Devin, Jules, or similar products into the core route;
- automatic severity, root-cause, release, merge, or external-write decisions;
- persistent conversation, evidence, model output, or QA memory.
