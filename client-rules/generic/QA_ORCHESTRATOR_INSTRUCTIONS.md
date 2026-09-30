# QA Orchestrator host-agent instructions

Use `qa-orchestrator` as a deterministic helper for QA profiles and bounded, content-free orchestration state. The primary host agent remains the sole owner of evidence, analysis, decisions, and external actions.

This file is the canonical source for orchestration mechanics and the generic evidence, finding, and final-output contract. Workspace and repository rules may add local routing, safety, and domain-specific requirements, but must preserve these generic evidence and finding standards and reference this file instead of duplicating orchestration mechanics.

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
- Keep one compact per-task Evidence Packet with stable `E1`, `E2`, ... references. Give each profile only relevant sections. `code_explorer` returns an evidence map and unresolved links, not defect candidates; `code_reviewer` uses that map and reports only concrete candidates supported by the changed code and evidence. Other profiles stay within their returned focus. Use stable `F-01`, `F-02`, ... IDs for bounded finding candidates; each candidate cites evidence references, states evidence confidence, and names its verification gap. Keep coverage gaps separate, deduplicate candidates during synthesis, and do not repeat full diffs or raw logs. A candidate is not a confirmed final finding until the host validates it.
- Routes and orchestration states are marked `read_only=true` and `host_owns_decisions=true`; do not treat the orchestrator as an autonomous agent or delegate external writes to it.

## Evidence and final findings

### Confirmation and traceability

- Put a defect in final `Findings` only when evidence supports both the failure condition and its material impact. Direct source and contract evidence can confirm a code-level defect; state clearly when runtime, deployment, or release behavior was not tested.
- Do not turn a missing test, unavailable environment, coverage gap, assumption, style preference, or speculative worst case into a finding. Put these under `Open Questions / Could Not Verify`, `Coverage Gaps`, or `Residual Risks` as appropriate.
- Every finding must point to a verifiable source. `E1`, `E2`, and similar IDs are useful inside orchestration, but the final report must map each ID to a direct locator: repository and file/line/commit, issue or MR link, test or pipeline run, or log source with time and environment. Do not leave evidence IDs unexplained.
- Keep evidence confidence separate from severity. If a candidate is too uncertain to assert as a defect, do not promote it to a confirmed finding; report the uncertainty and the missing verification separately.

### Severity

Severity describes evidenced user or business impact, not confidence, implementation effort, or release readiness by itself. Consider the affected flow and scope, the consequence, and whether a safe workaround exists. Use these levels consistently:

- `Blocker` — a confirmed failure makes a critical in-scope flow unusable or makes the affected behavior unsafe, such as loss or corruption of data or a serious security or transaction-integrity failure; no reasonable mitigation is available.
- `High` — a confirmed failure breaks a major user or business flow, or materially affects correctness or security for a meaningful in-scope group; there is no reasonable workaround.
- `Medium` — a confirmed functional problem is limited to particular conditions or users, or a workaround exists, while the core outcome remains possible.
- `Low` — a minor, narrow issue such as a cosmetic defect or rare edge case, with core functionality and data, security, and transaction correctness intact.

Do not raise severity because evidence is incomplete or because a worst-case impact is merely possible. State uncertain scope or impact in the verification gap. Report findings in severity order: `Blocker`, `High`, `Medium`, `Low`; localize the displayed severity values to the report language while preserving this order and meaning.

### Affected area

- Use the explicit field `Affected area` and name the actual component, service, interface, data flow, or user scenario supported by evidence. For a boundary issue, name both sides (for example, `API response → web checkout`). Add platform, role, or locale only when it is relevant and verified.
- Do not invent a universal set of domain labels or use an unqualified label such as `Consumer` unless a task-specific rule defines exactly what it means.

### Finding language

- Keep finding field names in English exactly as shown in the final format (`Finding ID`, `Severity`, `Affected area`, `Problem`, `Evidence`, `Impact`, `Evidence confidence`, `Verification gap`, and `Next step`).
- Write the finding title and field values in the user's language unless the user requests another language. For a Russian-language review, for example, use `Severity: Низкая` and write the title and explanation in Russian; do not translate the field name itself.
- Localize severity and evidence-confidence values while preserving their meaning (`Blocker` → `Блокирующая`, `High` → `Высокая`, `Medium` → `Средняя`, `Low` → `Низкая`). Keep IDs, code, paths, commit SHAs, and exact source or UI quotes unchanged; explain them in the report language.

### Final finding format

Use this format for each confirmed finding. Keep IDs stable across review stages and synthesis. Omit a field only when it is genuinely not applicable; do not add empty boilerplate.

Put each field on its own line; do not combine multiple fields on one line.

```text
Finding ID: F-01
Severity: High
Affected area: API response → web checkout
Problem: <trigger, actual behavior, and violated expectation>
Evidence: E1 — <direct source locator>; E2 — <direct source locator>
Impact: <who or what is affected and the concrete consequence>
Evidence confidence: <High or Medium, with a brief reason>
Verification gap: <what was not checked, or None for the reviewed scope>
Next step: <targeted verification or correction>
```

Evidence confidence describes how directly the available evidence supports the finding, not how serious the impact is:

- `High` — direct source, contract, or test evidence supports the failure condition and expected behavior.
- `Medium` — the failure condition is supported, but runtime behavior or the extent of impact remains unverified; name that gap.
- `Low` — a key causal link, expected behavior, or claimed impact is still inferred. Keep it as an open question or unverified candidate, not a confirmed finding.

Severity and evidence confidence answer different questions. A high-impact claim with low confidence is not a high-severity confirmed finding.

Illustrative examples:

```text
Finding ID: F-01
Severity: High
Affected area: payment API → checkout client
Problem: The API contract now returns major currency units, but the client still divides by 100.
Evidence: E1 — changed API contract; E2 — client conversion at the affected call site; E3 — no alternate flow for this supported route.
Impact: Users on this supported checkout path cannot submit a correct amount, and no safe alternate route is available.
Evidence confidence: High — both sides of the contract are visible in source.
Verification gap: End-to-end payment behavior was not run.
Next step: Add a focused contract or integration check for the conversion.

Finding ID: F-02
Severity: Низкая
Affected area: экран настроек на узкой ширине
Problem: Подпись отображается не полностью, но элемент управления остаётся доступным.
Evidence: E3 — скриншот на затронутой ширине; E4 — соответствующее правило разметки.
Impact: Небольшой визуальный дефект ограничен этим экраном и размером.
Evidence confidence: Высокая — обрезание видно на скриншоте.
Verification gap: Другие языки и размеры экрана не проверялись.
Next step: После исправления проверить подпись на минимальной поддерживаемой ширине.
```

For implementation-aware reviews, list confirmed findings first, then a concise change summary, a targeted verification or manual test plan, and open questions or unverified risks. If none are confirmed, say so only for the scope actually reviewed; do not imply untested areas are clear.

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

The orchestrator publishes exactly nine tools: `prepare_qa_orchestration`, `get_qa_orchestration_catalog`, `get_qa_orchestration_model_policy`, `start_qa_orchestration`, `advance_qa_orchestration`, `get_qa_orchestration`, `list_qa_orchestrations`, `finish_qa_orchestration`, and `delete_qa_orchestration`.

Use `delete_qa_orchestration(run_id)` only when the user explicitly wants to discard
that session; never as automatic cleanup or a substitute for normal finalization.
Deletion immediately removes memory and configured recovery storage at any stage,
records no QA outcome, and does not stop host tasks or model executions. The ID
cannot be resumed or restored. `deleted=true` confirms absence, including for an
already missing ID; retries are safe. If storage fails, the memory state remains.

When available profiles, bundle purposes, or route order are unclear before
triage, call `get_qa_orchestration_catalog` with no arguments. It returns the
fixed profile and bundle catalog without creating a session. Task-based
recommendations are shortlists only; select from the changed files and confirmed
stack. Use `prepare_qa_orchestration` for the selected profile's detailed
checklist, and `advance_qa_orchestration` to apply the chosen route at triage.
Catalog reads do not change sessions, TTL, or storage.

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
