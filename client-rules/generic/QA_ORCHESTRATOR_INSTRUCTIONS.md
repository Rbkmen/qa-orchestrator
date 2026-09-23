# QA Orchestrator host-agent instructions

Use `qa-orchestrator` as a deterministic helper for QA profiles and anonymized task metrics. The primary host agent remains the sole owner of evidence, analysis, decisions, and external actions.

This file is the canonical source for orchestrator mechanics. Workspace and repository rules own task-specific routing and output shape; they should reference this file instead of duplicating its mechanics.

## Availability and failure fallback

- Use these instructions only when the `qa-orchestrator` MCP is available and
  the task is an applicable QA flow. If the MCP is unavailable, a required call
  fails, or the session expires, continue with the selected workspace and
  repository rules, mark orchestration and metrics as unavailable, and never
  emulate or claim an orchestration call.
- A partial result caused by missing orchestration input remains a valid
  host-owned QA result; report the missing boundary.

## Review routing

- For an implementation-aware QA review, start with `start_qa_orchestration`. During Luna triage, select exactly one fixed bundle or one compatibility profile; never send `selected_bundle` and `selected_profile` together. Then call `prepare_review_route` for each selected profile: `pr_test_analyzer`, `code_reviewer`, `security_reviewer`, `silent_failure_hunter`, `code_explorer`, `typescript_reviewer`, or `react_reviewer`.
- For a low-risk, one-repository change with one narrow concern, prefer one compatibility profile to save tokens: `code_reviewer` for behavior/callers, `pr_test_analyzer` for test-only changes, `typescript_reviewer` for TypeScript-only changes, or `react_reviewer` for React-only changes. Use a fixed bundle for broad, cross-concern, or high-risk work.
- Available bundles are `ordinary_mr` (`code_explorer` → `code_reviewer` → `pr_test_analyzer`), `widget` (`code_explorer` → `react_reviewer` → `typescript_reviewer` → `pr_test_analyzer`), `security` (`code_explorer` → `security_reviewer` → `silent_failure_hunter`), `autotest` (`code_reviewer` → `pr_test_analyzer` → `typescript_reviewer`), and `requirements` (`code_explorer` → `code_reviewer`). Do not change the order or submit a custom list.
- Role display names are `Faraday — Evidence Investigator` = `code_explorer`, `Code Reviewer`, `Test Analyzer`, `Security Reviewer`, `Silent Failure Hunter`, `TypeScript Reviewer`, and `React Reviewer`. Faraday is an internal role name, not an external service or separate model.
- Use the returned `focus`, `required_sections`, `constraints`, and `escalation_signals` as the working checklist.
- Run stages under the returned `model_policy`: the configured triage model with its selected reasoning/effort, primary model with its selected reasoning/effort, optional deep model with its selected reasoning/effort (default `high` when supported), and synthesis model with its selected reasoning/effort; keep `speed=1.0` for every stage. The default setup is OpenAI/Codex with `gpt-6-luna/max`, `gpt-6-sol/medium`, and optional `gpt-6-sol/high`; `qa-orch setup` selects OpenAI or Anthropic, model IDs, and provider-specific reasoning/effort for triage, primary review, deep review, and synthesis. OpenAI uses `reasoning.effort`; Anthropic uses `output_config.effort`; `none` means omit the provider parameter. Show the host status using the returned model names; on the final primary-review role, pass `completed_profile` and the boolean `risk_signals` object in the same `advance_qa_orchestration` call. Do not send `risk_signals` on the later synthesis transition. On a match, add the configured deep model once before synthesis. After every stage, call `advance_qa_orchestration` with a structured signal; for each profile, pass `completed_profile`; use `get_qa_orchestration` for the next action. The `terra_primary_review` and `terra_synthesis` transition identifiers are unchanged; select the model from `model_policy`, not the step name.
- For status text, the default flow is `Luna / Max` → `Sol / Medium` per role → optional `Sol / High` → `Sol / Medium` synthesis → Host → Final QA outcome. When another policy is selected, show the configured model IDs instead of these default names.
- Obtain authoritative evidence from the issue tracker, code host, test-management system, observability, and code systems yourself; inspect the diff, and separate confirmed findings from hypotheses and unverified runtime or release facts.
- Keep one compact per-task Evidence Packet with stable `E1`, `E2`, ... references. Give each profile only relevant sections and return bounded candidates as `F-01`, `F-02`, ... with evidence references, confidence, and verification gaps; do not repeat full diffs or raw logs.
- For implementation-aware reviews, use the final format: Findings, Changes, Manual Test Plan, Open Questions / Could Not Verify. If a workspace flow explicitly defines another output shape for requirements or planning, follow that flow.
- Routes and orchestration states are marked `read_only=true` and `host_owns_decisions=true`; do not treat the orchestrator as an autonomous agent or delegate external writes to it.

## Optional deep analysis

On the final primary-review role, set `risk_signals` from the evidence state: `high_risk_domain`, `evidence_uncertain`, `cross_system_scope`, `multiple_plausible_causes`, `evidence_conflict`, `non_reproducible`, and `high_blast_radius`. Sol/high is selected when: high risk + uncertainty; at least two complexity signals; or a high-impact evidence conflict. Pass no raw evidence, logs, prompts, paths, or model output. Validate `deep_assessment` and the Sol response yourself, and do not create a second escalation automatically. The legacy `needs_deep_analysis` + `reason_code` path is compatibility-only.

## Metrics

- When the current QA task reaches its final reported status (`completed`,
  `partial`, or `blocked`), call `record_qa_task_outcome` exactly once. Do not
  record intermediate continuations; if the task resumes after a partial
  result, record only the final status for that task. For orchestration, pass
  the session `run_id` (this infers orchestration, so `orchestration_used` may
  be omitted), and for a regular task omit it. Do not pass
  `orchestration_used=false` with a `run_id`.
- Every outcome call requires `codegraph_calls`, `source_mcp_calls`, `findings_identified`, `findings_confirmed`, `findings_rejected`, and `repeated_source_reads`; pass `0` when a counter is empty.
- New outcome events use schema v2. Send stage calls `triage_calls`, `primary_review_calls`, `deep_review_calls`, and `synthesis_calls`, shared `orchestration_steps_completed` and `orchestration_retries`, and optional token counters `triage_input_tokens`, `triage_output_tokens`, `primary_review_input_tokens`, `primary_review_output_tokens`, `synthesis_input_tokens`, and `synthesis_output_tokens`. Deep-review token counters remain `deep_input_tokens` and `deep_output_tokens`.
- Only after deep review actually runs, send the configured `deep_model` and configured `deep_reasoning` (default `high`); if the task becomes `partial` or `blocked` before deep review starts, keep `deep_review_calls=0` and omit those fields. For a completed bundle with `N` profiles, send at least one triage call, `N` primary-review calls, one synthesis call, and `N+2` completed steps; add one deep-review call and one step when it ran.
- Stored schema-v1 history remains readable; its model-family totals are reported separately from the v2 stage totals.
- Never send issue keys, titles, paths, source text, code, logs, screenshots, prompts, or review responses.
- Use `get_metrics_report(days)` only for an aggregate read-only report.

The orchestrator publishes exactly six tools: `prepare_review_route`, `start_qa_orchestration`, `advance_qa_orchestration`, `get_qa_orchestration`, `record_qa_task_outcome`, and `get_metrics_report`.

Do not add persistent QA memory, a source cache, a learning layer, or hidden tool calls.
