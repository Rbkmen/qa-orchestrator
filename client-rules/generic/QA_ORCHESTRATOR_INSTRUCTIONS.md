# QA Orchestrator host-agent instructions

Use `qa-orchestrator` as a deterministic helper for QA profiles and anonymized task metrics. The primary host agent remains the sole owner of evidence, analysis, decisions, and external actions.

## Review routing

- For an implementation-aware QA review, start with `start_qa_orchestration`. During Luna triage, select exactly one fixed bundle or one compatibility profile; never send `selected_bundle` and `selected_profile` together. Then call `prepare_review_route` for each selected profile: `pr_test_analyzer`, `code_reviewer`, `security_reviewer`, `silent_failure_hunter`, `code_explorer`, `typescript_reviewer`, or `react_reviewer`.
- For a low-risk, one-repository change with one narrow concern, prefer one compatibility profile to save tokens: `code_reviewer` for behavior/callers, `pr_test_analyzer` for test-only changes, `typescript_reviewer` for TypeScript-only changes, or `react_reviewer` for React-only changes. Use a fixed bundle for broad, cross-concern, or high-risk work.
- Available bundles are `ordinary_mr` (`code_explorer` → `code_reviewer` → `pr_test_analyzer`), `widget` (`code_explorer` → `react_reviewer` → `typescript_reviewer` → `pr_test_analyzer`), `security` (`code_explorer` → `security_reviewer` → `silent_failure_hunter`), `autotest` (`code_reviewer` → `pr_test_analyzer` → `typescript_reviewer`), and `requirements` (`code_explorer` → `code_reviewer`). Do not change the order or submit a custom list.
- Role display names are `Faraday — Evidence Investigator` = `code_explorer`, `Code Reviewer`, `Test Analyzer`, `Security Reviewer`, `Silent Failure Hunter`, `TypeScript Reviewer`, and `React Reviewer`. Faraday is an internal role name, not an external service or separate model.
- Use the returned `focus`, `required_sections`, `constraints`, and `escalation_signals` as the working checklist.
- Run stages under the policy: `gpt-5.6-luna/max`, `gpt-5.6-terra/medium`, and optional `gpt-5.6-sol/high`; keep `speed=1.0` for every stage. Show host status as `Luna / Max → Ordinary MR Review` → `Terra / Medium` for each role → `Terra / Medium → Synthesis` → `Host → Final QA outcome`; after the last Terra role, pass only the boolean `risk_signals` object and let the orchestrator apply the fixed deep-review rules. On a match, add `Sol / High → Deep read-only review` only once before synthesis. After every stage, call `advance_qa_orchestration` with a structured signal; for each Terra profile, pass `completed_profile`; use `get_qa_orchestration` for the next action.
- Obtain Jira, MR, TestRail, monitoring, and code evidence yourself, inspect the diff, and separate confirmed findings from hypotheses and unverified runtime or release facts.
- Keep one compact per-task Evidence Packet with stable `E1`, `E2`, ... references. Give each profile only relevant sections and return bounded candidates as `F-01`, `F-02`, ... with evidence references, confidence, and verification gaps; do not repeat full diffs or raw logs.
- Always keep the final format: Findings, Changes, Manual Test Plan, Open Questions / Could Not Verify.
- Routes and orchestration states are marked `read_only=true` and `host_owns_decisions=true`; do not treat the orchestrator as an autonomous agent or delegate external writes to it.

## Optional deep analysis

After the final Terra role, set `risk_signals` from the evidence state: `high_risk_domain`, `evidence_uncertain`, `cross_system_scope`, `multiple_plausible_causes`, `evidence_conflict`, `non_reproducible`, and `high_blast_radius`. Sol/high is selected when: high risk + uncertainty; at least two complexity signals; or a high-impact evidence conflict. Pass no raw evidence, logs, prompts, paths, or model output. Validate `deep_assessment` and the Sol response yourself, and do not create a second escalation automatically. The legacy `needs_deep_analysis` + `reason_code` path is compatibility-only.

## Metrics

- After each QA task with status `completed`, `partial`, or `blocked`, call `record_qa_task_outcome` exactly once; for orchestration, pass the session `run_id` (this infers orchestration, so `orchestration_used` may be omitted), and for a regular task omit it. Do not pass `orchestration_used=false` with a `run_id`.
- Send only `task_type`, `outcome`, call counters, findings counters, repeated reads, scope counters, per-stage token counters, Sol-value counters, and measurable `deep_*` counters. For an orchestrated Sol branch, `deep_model=gpt-5.6-sol` and `deep_reasoning=high` are required.
- Never send issue keys, titles, paths, source text, code, logs, screenshots, prompts, or review responses.
- Use `get_metrics_report(days)` only for an aggregate read-only report.

The orchestrator publishes exactly six tools: `prepare_review_route`, `start_qa_orchestration`, `advance_qa_orchestration`, `get_qa_orchestration`, `record_qa_task_outcome`, and `get_metrics_report`.

Do not add persistent QA memory, a source cache, a learning layer, or hidden tool calls.
