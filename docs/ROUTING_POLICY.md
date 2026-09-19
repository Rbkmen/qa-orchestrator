# QA Router Policy

This policy is client-neutral. In this document, **host agent** means Codex, Claude Code, Cursor Agent, or another MCP-capable AI that calls QA Router.

## Responsibility boundary

The host agent owns:

- task classification and source retrieval;
- requirement, code, diff, and log analysis;
- the final coverage map, risk, severity, and release judgment;
- all code and file changes;
- all writes to Jira, GitLab, TestRail, Sentry, or any other external system;
- review and correction of every local draft.

Qwen3.5-9B produces bounded drafts only. Its output is never evidence, a final QA decision, or authorization for an external action.

The ECC-inspired review profiles are checklist modes inside QA Router, not
autonomous agents. They do not retrieve sources, call other agents, write
files, change Git, or update Jira, GitLab, TestRail, Sentry, or any other
external system.

## Automatic routing thresholds

For every sanitized routine draft, first check this list. Call QA Router when one condition matches:

- 2–12 approved test cases based on a coverage map already approved by the host agent;
- sanitized logs containing at least 3,000 characters;
- source-bound summaries containing at least 2,000 characters;
- translations or rewrites containing at least 1,000 characters;
- automation skeletons with an explicit project pattern and a multi-step scenario.

Keep smaller tasks in the host agent. Use `explain_short` locally only when the user explicitly requests the local model. An explicit request may override a size threshold, but never a security or validation rule.

## Data minimization

Before every local call, the host agent must prepare the smallest sufficient packet.

Never send:

- credentials, cookies, tokens, or private keys;
- personal, session, identity, or payment data;
- unrestricted corporate documents;
- complete repositories or full chat history;
- raw external-system payloads;
- raw QA logs before reduction and sanitization.

If the router returns `sensitive_data_detected`, use only its coarse `sensitive_category` diagnostic to reduce and sanitize the packet. The matching value is never returned or logged. Never weaken or bypass the policy check.

## Tool selection

- `draft_test_cases`: expand an approved 1–12 item coverage map. Each item must contain a unique stable `coverage_id` in `COV-*` format, one purpose, confirmed source, state or branch, and expected invariant. The result must repeat every supplied ID exactly once.
- `draft_review_checklist`: select one static profile — `pr_test_analyzer`, `code_reviewer`, `security_reviewer`, `silent_failure_hunter`, `code_explorer`, `typescript_reviewer`, or `react_reviewer` — and send only a bounded sanitized Evidence Packet plus an optional project pattern. The result is an unverified checklist with `Scope`, `Checklist`, `Candidate Coverage Gaps`, `Positive Observations`, and `Unverified`; it is not a finding, severity, root-cause, release/merge decision, or runtime proof.
- `summarize_logs`: group only visible signatures. Do not accept an inferred root cause without separate evidence.
- `draft_automation_skeleton`: draft structure from an explicit project pattern. The tool must not write files or external data; generated Python snippets are checked for file, process, and network mutations.
- `translate_text`: translate sanitized text while preserving supplied terminology. Every supplied preserve term must appear verbatim in the returned draft.
- `rewrite_text`: shorten, correct, or restyle supplied text without adding facts.
- `summarize_text`: summarize only supplied source material.
- `explain_short`: explain stable, non-researched material only after an explicit local-model request.

## Review contract

After every successful draft:

1. Compare it with the bounded input and authoritative evidence.
2. Remove unsupported facts, invented behavior, missing branches, and merged scenarios.
3. Keep assumptions and unverifiable statements explicitly marked.
4. Return only the host agent's reviewed result to the user.

If `canary_feedback_required` is true, call `record_canary_feedback` exactly once with the returned `draft_id`:

- unchanged: `accepted` and `none`;
- corrected: `edited` and the primary correction category;
- discarded: `rejected` and the primary rejection category.

Do not invent, reuse, or persist draft IDs. If the router refuses or falls back, continue in the host agent without retry loops.

`quality_status` controls automatic use per tool:

- `active`: normal automatic routing;
- `canary`: route normally, but review feedback is requested until enough evidence exists;
- `paused`: do not use that local route; continue in the host agent.

The gate uses the latest 20 reviewed drafts for the current profile. Every tool requests feedback for its first 10 reviewed drafts, even when its initial route is active. From 10 reviews, at least 80% acceptable drafts activate the route, while at least 20% serious factual or coverage corrections pause it. Translation, rewrite, source-bound summary, and short explanation start active; test cases, log summaries, and automation skeletons start in canary.

When `shadow_evaluation_required` is true, create a separate host-agent baseline from the same sanitized Evidence Packet without incorporating the Qwen draft, compare both outputs, and use that comparison for the required content-free feedback. This flag is selected deterministically for approximately 10% of successful interactive drafts. It is a practical shadow check rather than a blind experiment because the flag arrives with the local result. QA Router never starts an extra cloud request itself.

After every QA task reaches `completed`, `partial`, or `blocked`, call `record_qa_task_outcome` exactly once. Send counters and booleans only; never include ticket IDs, titles, source text, code, logs, paths, or draft content. Use `deep_analysis_used`, `deep_model`, and `deep_reasoning` for optional client-owned deep analysis; include `deep_duration_ms`, `deep_input_tokens`, and `deep_output_tokens` when measurable. The legacy `sol_used` input remains accepted for older clients. `source_mcp_calls` counts Jira, GitLab, TestRail, Sentry, Grafana, OpenSearch, Slack, and Confluence retrieval only; exclude CodeGraph, QA Router calls, and the outcome call itself.

When measurable, also send token counters: `codegraph_response_tokens`, `source_mcp_response_tokens`, and `avoided_source_read_tokens`. They are aggregate counts only. Omit a counter when it cannot be measured; `0` means it was measured and its actual value was zero. `avoided_source_read_tokens` is an estimate of source output that was not retrieved because CodeGraph answered the same bounded question; do not report it as an exact counterfactual.

Generation metrics include explicit `token_usage_available` and `phase_latency_available` flags. The report counts a measurement as complete only when its flag is true; zero-filled fields on refusals, transport failures, or calls without provider usage do not count as measured data. A zero `repair_ms` on a complete event means that no repair was needed.

## Optional deep analysis

Deep-analysis agents are client-owned and are not part of QA Router. A host may use one bounded read-only specialist for difficult cross-repository reasoning, conflicting evidence, security-sensitive work, or high-blast-radius edge cases. Sol can remain the control while Astra is evaluated as a canary on identical evidence packets. The host still makes the final decision.

## Persistence

QA Router is stateless at the application level. Do not add a learning layer, persistent model memory, or local copies of source-system content.
