# QA Orchestrator host-agent instructions

Use `qa-orchestrator` as a deterministic helper for QA profiles and bounded, content-free orchestration state. The primary host agent remains the sole owner of evidence, analysis, decisions, and external actions.

This file is the canonical source for orchestrator mechanics. Workspace and repository rules own task-specific routing and output shape; they should reference this file instead of duplicating its mechanics.

## Availability and failure fallback

- Use these instructions only when the `qa-orchestrator` MCP is available and
  the task is an applicable QA flow. If the MCP is unavailable, a required call
  fails, or the session expires, continue with the selected workspace and
  repository rules, mark orchestration as unavailable, and never emulate or
  claim an orchestration call.
- A partial result caused by missing orchestration input remains a valid
  host-owned QA result; report the missing boundary.

## Review routing

- For an implementation-aware QA review, start with `start_qa_orchestration`. During triage, select exactly one fixed bundle or one compatibility profile; never send `selected_bundle` and `selected_profile` together. Then call `prepare_qa_orchestration` for each selected profile: `pr_test_analyzer`, `code_reviewer`, `security_reviewer`, `silent_failure_hunter`, `code_explorer`, `typescript_reviewer`, `react_reviewer`, `ruby_reviewer`, `python_reviewer`, or `mobile_reviewer`.
- Treat returned `recommended_bundles` as a task-type shortlist, not a risk score or required choice; `allowed_bundles` remains the complete set. Inspect the changed paths and confirmed project stack before choosing the initial route.
- Default to one compatibility profile for routine, narrowly scoped, low-risk changes in one repository: `code_reviewer` for behavior/callers, `pr_test_analyzer` for test-only changes, `typescript_reviewer` for TypeScript-only changes, `react_reviewer` for React-only changes, `ruby_reviewer` for Ruby-only changes, `python_reviewer` for Python/MCP-only changes, or `mobile_reviewer` for React Native/native-platform-only changes. Use a fixed bundle for broad, cross-concern, or high-risk work. The host selects the initial route; risk signals are evaluated after primary review and only control optional deep review.
- Available bundles are `ordinary_mr` (`code_explorer` → `code_reviewer` → `pr_test_analyzer`), `widget` (`code_explorer` → `react_reviewer` → `typescript_reviewer` → `pr_test_analyzer`), `widget_js` (`code_explorer` → `code_reviewer` → `react_reviewer` → `pr_test_analyzer`), `ruby_backend` (`code_explorer` → `ruby_reviewer` → `pr_test_analyzer`), `python_backend` (`code_explorer` → `python_reviewer` → `pr_test_analyzer`), `mobile` (`code_explorer` → `mobile_reviewer` → `pr_test_analyzer`), `security` (`code_explorer` → `security_reviewer` → `silent_failure_hunter`), `autotest` (`code_reviewer` → `pr_test_analyzer` → `typescript_reviewer`), and `requirements` (`code_explorer` → `code_reviewer`). Do not change the order or submit a custom list.
- `autotest` is for broad TypeScript automation changes; use `ordinary_mr` for broad non-TypeScript automation. `widget` is for TypeScript React changes; use `widget_js` for broad JavaScript React changes; use `ruby_backend` for broad Ruby backend changes; use `python_backend` for broad Python/MCP work; use `mobile` for broad React Native/native iOS/Android work. Choose from changed paths and confirmed manifests, not repository name alone; for a mixed-stack diff, use the fixed bundle matching the broadest or highest-risk surface and note uncovered stack checks in the host verification plan.
- Role display names are `Faraday — Evidence Investigator` = `code_explorer`, `Code Reviewer`, `Test Analyzer`, `Security Reviewer`, `Silent Failure Hunter`, `TypeScript Reviewer`, `React Reviewer`, `Ruby Reviewer`, `Python Reviewer`, and `Mobile Reviewer`. Faraday is an internal role name, not an external service or separate model.
- Use the returned `focus`, `required_sections`, `constraints`, and `escalation_signals` as the working checklist. Follow the route's sections; do not add boilerplate or fill sections with empty positive observations.
- Use the model and reasoning returned in `model_policy` for every stage. `reasoning_capabilities_verified` is true only for an exact locally curated model/provider pair; false means verify the selected effort with the provider. The flag does not check live model access or capabilities. Configure provider and model IDs with `qa-orch setup`. Execution speed and latency follow the user's host/provider settings; the orchestrator does not set or override them. OpenAI uses `reasoning.effort`; Anthropic uses `output_config.effort`; `none` disables reasoning on OpenAI reasoning models that support it; omit the parameter for non-reasoning models such as GPT-4.1. For Anthropic, `none` is a local sentinel to omit effort and use model defaults. The setup wizard does not contact provider APIs, so verify model access and custom-ID reasoning/effort support with the host provider.
- Keep stage labels and status text model-neutral: `Triage` → `Primary review` → optional `Deep review` → `Final synthesis` → `Host` → `Final QA outcome`. Do not add model names or reasoning levels. Transition identifiers are also model-neutral; select execution settings from `model_policy`, not from the transition name.
- On the final primary-review role, pass `completed_profile` and the structured boolean `risk_signals` object in the same `advance_qa_orchestration` call. Send `{}` when no signals apply. Do not send `risk_signals` on the later synthesis transition.
- After each stage, call `advance_qa_orchestration` with its required transition fields; pass `completed_profile` for each profile. Use the updated session returned by `advance_qa_orchestration` for the next action and model policy. Call `get_qa_orchestration` only to resume or recover an interrupted or unclear session. If escalation is selected, run the configured deep stage once before synthesis.
- Obtain authoritative evidence from the issue tracker, code host, test-management system, observability, and code systems yourself; inspect the diff, and separate confirmed findings from hypotheses and unverified runtime or release facts.
- Keep one compact per-task Evidence Packet with stable `E1`, `E2`, ... references. Give each profile only relevant sections. `code_explorer` returns an evidence map and unresolved links, not defect candidates; `code_reviewer` uses that map and reports only concrete candidates supported by the changed code and evidence. Other profiles stay within their returned focus. Use `F-01`, `F-02`, ... for bounded finding candidates, each with evidence references, confidence, and a verification gap; keep coverage gaps separate and do not repeat full diffs or raw logs.
- Routes and orchestration states are marked `read_only=true` and `host_owns_decisions=true`; do not treat the orchestrator as an autonomous agent or delegate external writes to it.

## Optional deep analysis

On the final primary-review role, set `risk_signals` from the evidence state. Map only conditions actually supported by evidence: sensitive identity, security, payment, fraud, privacy, or access-control impact → `high_risk_domain`; a material missing verification source → `evidence_uncertain`; a relevant boundary crossing repositories, services, or systems → `cross_system_scope`; multiple plausible causes remaining after investigation → `multiple_plausible_causes`; conflicting authoritative evidence → `evidence_conflict`; a reported behavior that cannot be reproduced under expected conditions → `non_reproducible`; broad impact across consumers or data → `high_blast_radius`. A profile's escalation text or an ordinary coverage gap alone does not automatically set a signal or trigger deep review. Deep review is selected only when a fixed rule matches: high risk + uncertainty; at least two complexity signals; or a high-impact evidence conflict. Pass no raw evidence, logs, prompts, paths, or model output. Validate `deep_assessment` and the deep-review findings yourself, and do not create a second escalation automatically.

## Finalize an orchestration

- After synthesis, call `finish_qa_orchestration` once with the session `run_id`
  and the host-owned final outcome (`completed`, `partial`, or `blocked`). For
  an early stop, pass the same `partial` or `blocked` outcome already sent to
  `advance_qa_orchestration`.
- By default, session state stays in memory. If `QA_ORCHESTRATOR_SESSION_STORE_PATH`
  is configured, unfinished structured state can be recovered after a restart;
  finalization deletes its stored row. Final outcomes are not persisted. Do not
  call this for a task that was not orchestrated. Repeating the same outcome is
  idempotent in the current process; a conflicting final outcome is rejected.

The orchestrator publishes exactly seven tools: `prepare_qa_orchestration`, `get_qa_orchestration_model_policy`, `start_qa_orchestration`, `advance_qa_orchestration`, `get_qa_orchestration`, `list_qa_orchestrations`, and `finish_qa_orchestration`.

If a session's `run_id` is lost, call `list_qa_orchestrations` with no arguments.
Identify the intended entry by task type, route, stage, and expiry, then call
`get_qa_orchestration` with its ID. Ask the user which session to resume if several
entries match; do not guess or create another session before checking. Listing
includes retained terminal sessions, excludes expired/evicted sessions, and does
not renew TTL or write storage. After restart, only unfinished sessions restored
from configured local storage are available.

When asked which models are loaded, call `get_qa_orchestration_model_policy`
with no arguments. It returns the current server's policy for new sessions,
without reading disk or starting a session. After `qa-orch setup`, restart the
server connection before checking. CLI `config show` and `reload` read disk;
they do not inspect or reload the connected process. Verify actual model
execution in the host. For an existing recovered session, follow that session's
`model_policy`; its next transition uses the restarted server's policy.

Do not add persistent QA memory, a source cache, a learning layer, or hidden tool calls.
