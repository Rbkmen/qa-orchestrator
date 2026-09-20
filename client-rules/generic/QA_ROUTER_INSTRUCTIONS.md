# QA Router host-agent instructions

Use `qa-router` as a deterministic helper for QA profiles and anonymized task metrics. The primary host agent remains the sole owner of evidence, analysis, decisions, and external actions.

## Review routing

- For an implementation-aware QA review, start with `start_qa_orchestration`. During Luna triage, select one fixed bundle or one compatibility profile, then call `prepare_review_route` for each selected profile: `pr_test_analyzer`, `code_reviewer`, `security_reviewer`, `silent_failure_hunter`, `code_explorer`, `typescript_reviewer`, or `react_reviewer`.
- Available bundles are `ordinary_mr` (`code_explorer` → `code_reviewer` → `pr_test_analyzer`), `widget` (`code_explorer` → `react_reviewer` → `typescript_reviewer` → `pr_test_analyzer`), `security` (`code_explorer` → `security_reviewer` → `silent_failure_hunter`), `autotest` (`code_reviewer` → `pr_test_analyzer` → `typescript_reviewer`), and `requirements` (`code_explorer` → `code_reviewer`). Do not change the order or submit a custom list.
- Role display names are `Faraday — Evidence Investigator` = `code_explorer`, `Code Reviewer`, `Test Analyzer`, `Security Reviewer`, `Silent Failure Hunter`, `TypeScript Reviewer`, and `React Reviewer`. Faraday is an internal role name, not an external service or separate model.
- Use the returned `focus`, `required_sections`, `constraints`, and `escalation_signals` as the working checklist.
- Run stages under the policy: `gpt-5.6-luna/max` for triage, `gpt-5.6-terra/medium` for primary review of every role in bundle order and for synthesis, and optional `gpt-5.6-sol/high` for read-only deep analysis. Show host status as `Luna / Max → Ordinary MR Review` → `Terra / Medium` for each role → `Terra / Medium → Synthesis` → `Host → Final QA outcome`; on escalation, add `Sol / High → Deep read-only review` only after the last role. After every stage, call `advance_qa_orchestration` with a structured signal; for each Terra profile, pass `completed_profile`; use `get_qa_orchestration` for the next action.
- Obtain Jira, MR, TestRail, monitoring, and code evidence yourself, inspect the diff, and separate confirmed findings from hypotheses and unverified runtime or release facts.
- Always keep the final format: Findings, Changes, Manual Test Plan, Open Questions / Could Not Verify.
- Routes and orchestration states are marked `read_only=true` and `host_owns_decisions=true`; do not treat the router as an autonomous agent or delegate external writes to it.

## Optional deep analysis

For difficult cross-repository reasoning, debugging, security/payment/fraud-sensitive analysis, or high-blast-radius edge cases, the host may request an optional Sol/high escalation with one fixed `reason_code`. Validate the response yourself and do not create a second escalation automatically.

## Metrics

- After each QA task with status `completed`, `partial`, or `blocked`, call `record_qa_task_outcome` exactly once; for orchestration, pass the session `run_id`, and for a regular task omit it.
- Send only `task_type`, `outcome`, call counters, findings counters, repeated reads, and measurable `deep_*`/token counters. For an orchestrated Sol branch, `deep_model=gpt-5.6-sol` and `deep_reasoning=high` are required.
- Never send issue keys, titles, paths, source text, code, logs, screenshots, prompts, or review responses.
- Use `get_metrics_report(days)` only for an aggregate read-only report.

The router publishes exactly six tools: `prepare_review_route`, `start_qa_orchestration`, `advance_qa_orchestration`, `get_qa_orchestration`, `record_qa_task_outcome`, and `get_metrics_report`.

Do not add persistent QA memory, a source cache, a learning layer, or hidden tool calls.
