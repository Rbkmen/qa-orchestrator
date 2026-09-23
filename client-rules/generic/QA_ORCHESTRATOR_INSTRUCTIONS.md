# QA Orchestrator host-agent instructions

Use `qa-orchestrator` as a deterministic helper for QA profiles and anonymized task distribution. The primary host agent remains the sole owner of evidence, analysis, decisions, and external actions.

This file is the canonical source for orchestrator mechanics. Workspace and repository rules own task-specific routing and output shape; they should reference this file instead of duplicating its mechanics.

## Availability and failure fallback

- Use these instructions only when the `qa-orchestrator` MCP is available and
  the task is an applicable QA flow. If the MCP is unavailable, a required call
  fails, or the session expires, continue with the selected workspace and
  repository rules, mark orchestration and task-distribution recording as unavailable, and never
  emulate or claim an orchestration call.
- A partial result caused by missing orchestration input remains a valid
  host-owned QA result; report the missing boundary.

## Review routing

- For an implementation-aware QA review, start with `start_qa_orchestration`. During triage, select exactly one fixed bundle or one compatibility profile; never send `selected_bundle` and `selected_profile` together. Then call `prepare_review_route` for each selected profile: `pr_test_analyzer`, `code_reviewer`, `security_reviewer`, `silent_failure_hunter`, `code_explorer`, `typescript_reviewer`, or `react_reviewer`.
- For a low-risk, one-repository change with one narrow concern, prefer one compatibility profile: `code_reviewer` for behavior/callers, `pr_test_analyzer` for test-only changes, `typescript_reviewer` for TypeScript-only changes, or `react_reviewer` for React-only changes. Use a fixed bundle for broad, cross-concern, or high-risk work.
- Available bundles are `ordinary_mr` (`code_explorer` → `code_reviewer` → `pr_test_analyzer`), `widget` (`code_explorer` → `react_reviewer` → `typescript_reviewer` → `pr_test_analyzer`), `security` (`code_explorer` → `security_reviewer` → `silent_failure_hunter`), `autotest` (`code_reviewer` → `pr_test_analyzer` → `typescript_reviewer`), and `requirements` (`code_explorer` → `code_reviewer`). Do not change the order or submit a custom list.
- Role display names are `Faraday — Evidence Investigator` = `code_explorer`, `Code Reviewer`, `Test Analyzer`, `Security Reviewer`, `Silent Failure Hunter`, `TypeScript Reviewer`, and `React Reviewer`. Faraday is an internal role name, not an external service or separate model.
- Use the returned `focus`, `required_sections`, `constraints`, and `escalation_signals` as the working checklist.
- Run every stage with the model and reasoning returned in `model_policy`; keep `speed=1.0`. Configure the provider and model IDs in `qa-orch setup`. OpenAI uses `reasoning.effort`; Anthropic uses `output_config.effort`; `none` means omit the provider parameter. The setup wizard does not contact provider APIs; verify model access and custom-ID reasoning/effort support with the host provider. Keep stage labels and status text model-neutral: `Triage` → `Primary review` → optional `Deep review` → `Final synthesis` → `Host` → `Final QA outcome`; do not add model names or reasoning levels to them. On the final primary-review role, you must pass `completed_profile` and the structured boolean `risk_signals` object in the same `advance_qa_orchestration` call; send `{}` when no signals apply. Do not send `risk_signals` on the later synthesis transition. If escalation is selected, run the configured deep stage once before synthesis. After every stage, call `advance_qa_orchestration` with a structured signal; for each profile, pass `completed_profile`; use `get_qa_orchestration` for the next action. Transition identifiers are model-neutral; select execution settings from `model_policy`, not from the transition name.
- Obtain authoritative evidence from the issue tracker, code host, test-management system, observability, and code systems yourself; inspect the diff, and separate confirmed findings from hypotheses and unverified runtime or release facts.
- Keep one compact per-task Evidence Packet with stable `E1`, `E2`, ... references. Give each profile only relevant sections and return bounded candidates as `F-01`, `F-02`, ... with evidence references, confidence, and verification gaps; do not repeat full diffs or raw logs.
- For implementation-aware reviews, use the final format: Findings, Changes, Manual Test Plan, Open Questions / Could Not Verify. If a workspace flow explicitly defines another output shape for requirements or planning, follow that flow.
- Routes and orchestration states are marked `read_only=true` and `host_owns_decisions=true`; do not treat the orchestrator as an autonomous agent or delegate external writes to it.

## Optional deep analysis

On the final primary-review role, set `risk_signals` from the evidence state: `high_risk_domain`, `evidence_uncertain`, `cross_system_scope`, `multiple_plausible_causes`, `evidence_conflict`, `non_reproducible`, and `high_blast_radius`. Deep review is selected when: high risk + uncertainty; at least two complexity signals; or a high-impact evidence conflict. Pass no raw evidence, logs, prompts, paths, or model output. Validate `deep_assessment` and the deep-review findings yourself, and do not create a second escalation automatically. The legacy `needs_deep_analysis` + `reason_code` path is compatibility-only.

## Task distribution

- When the current QA task reaches its final status (`completed`, `partial`,
  or `blocked`), call `record_qa_task_outcome` exactly once. Do not
  record intermediate continuations; if the task resumes after a partial
  result, record only the final status for that task. For orchestration, pass
  the session `run_id`; for a regular task, omit it. Send only `task_type`,
  the final `outcome`, and `run_id` when applicable. The outcome is used to
  finalize an orchestrated session but is not saved in the distribution data.
- For non-orchestrated tasks, records are not deduplicated. Do not retry after
  an uncertain response; aggregate counts represent successful record calls,
  not verified unique tasks.
- The distribution record stores only the task type and timestamp. Do not send
  additional fields.
- `get_metrics_report(days)` returns only `period_days`, `total_tasks`, and
  `by_task_type`.
- Never send issue keys, titles, paths, source text, code, logs, screenshots, prompts, or review responses.
- Use `get_metrics_report(days)` only for an aggregate read-only report.

The orchestrator publishes exactly six tools: `prepare_review_route`, `start_qa_orchestration`, `advance_qa_orchestration`, `get_qa_orchestration`, `record_qa_task_outcome`, and `get_metrics_report`.

Do not add persistent QA memory, a source cache, a learning layer, or hidden tool calls.
