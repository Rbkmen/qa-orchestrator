# QA Orchestrator host-agent instructions

Use `qa-orchestrator` for fixed QA routing and bounded, content-free session state. The host owns evidence, final decisions, and external writes. This file is the canonical generic
orchestration/evidence/finding contract. Workspace and repository rules may add local requirements but must preserve it.

## Availability and failure fallback

- Use these instructions only when the `qa-orchestrator` MCP is available for an applicable QA flow. If the MCP is unavailable, a required call fails, or the session expires,
  follow workspace/repository rules, mark orchestration unavailable, and never imitate or claim an MCP call. A partial host-owned result is valid if the missing boundary is stated.

## Review routing

- For implementation-aware QA, call `start_qa_orchestration`; select exactly one fixed bundle or compatibility profile, never both. The successful triage advance returns
  `selected_review_routes` with checklists and `execution_groups`; use them without a separate preparation call. Use `prepare_qa_orchestration` or `prepare_qa_orchestration_bundle`
  only for standalone lookup/recovery. Available profiles: `pr_test_analyzer`, `code_reviewer`, `security_reviewer`, `silent_failure_hunter`, `code_explorer`,
  `typescript_reviewer`, `react_reviewer`, `ruby_reviewer`, `python_reviewer`, `mobile_reviewer`.
- Treat `recommended_bundles` as a task-type shortlist, not a risk score or requirement; `allowed_bundles` is the complete set. Choose from changed paths and confirmed stack, not
  repository name. A narrow, low-risk single concern uses one profile: behavior/callers → `code_reviewer`; test-only → `pr_test_analyzer`; TypeScript-only → `typescript_reviewer`;
  React-only → `react_reviewer`; Ruby-only → `ruby_reviewer`; Python/MCP-only → `python_reviewer`; React Native/native-platform-only → `mobile_reviewer`. Several changed files
  alone do not justify a bundle. Use a fixed bundle for broad, cross-concern, or high-risk work; risk signals affect only optional deep review after primary review.
- Fixed routes and execution groups (groups are barriers; `→` is order, `+` means same group):

| Bundle | Route and execution groups |
|---|---|
| `ordinary_mr` | `code_explorer` → `code_reviewer` + `pr_test_analyzer` |
| `widget` | `code_explorer` → `react_reviewer` + `typescript_reviewer` + `pr_test_analyzer` |
| `widget_js` | `code_explorer` → `code_reviewer` + `react_reviewer` + `pr_test_analyzer` |
| `ruby_backend` | `code_explorer` → `ruby_reviewer` + `pr_test_analyzer` |
| `python_backend` | `code_explorer` + `python_reviewer` + `pr_test_analyzer` (one parallel-start group) |
| `mobile` | `code_explorer` → `mobile_reviewer` + `pr_test_analyzer` |
| `security` | `code_explorer` → `security_reviewer` + `silent_failure_hunter` |
| `autotest` | `code_reviewer` + `typescript_reviewer` + `pr_test_analyzer` |
| `requirements` | `code_explorer` → `code_reviewer` |

- `autotest` is for broad TypeScript automation; use `ordinary_mr` for broad non-TypeScript automation. `widget` is TypeScript React; `widget_js` is broad JavaScript React;
  `ruby_backend`, `python_backend`, and `mobile` are for broad Ruby, Python/MCP, and React Native/native iOS/Android work. For mixed stacks choose the bundle for the
  broadest/highest-risk changed surface and state uncovered checks. Never invent, reorder, or submit a custom route. The returned catalog is authoritative.
- Display names: `Faraday — Evidence Investigator` = `code_explorer`; Code Reviewer, Test Analyzer, Security Reviewer, Silent Failure Hunter, TypeScript Reviewer, React Reviewer,
  Ruby Reviewer, Python Reviewer, and Mobile Reviewer. Faraday is an internal role name, not a service or model.
- Use returned `focus`, `required_sections`, `constraints`, and `escalation_signals` as the checklist; do not add boilerplate or empty positive observations. `code_explorer` maps
  evidence and unresolved links, not defects; `code_reviewer` uses that map for concrete candidates; other profiles stay within focus. The test analyzer reports test/coverage gaps,
  not implementation defects.
- Run each returned `execution_group` as a barrier; flattening groups must equal route order. Profiles in the same group may run concurrently when host workers support it. Give
  every temporary read-only worker the shared compact Evidence Packet, relevant deduplicated results from prior groups, and only its own checklist; use the primary-review
  model/reasoning from `model_policy`. Workers do not make source-MCP or CodeGraph calls or writes, decide severity, own session state, or rely on same-group results; return
  evidence gaps to the parent. The parent owns evidence retrieval, orchestration, candidate validation, synthesis, and the final decision. If workers are unavailable or at
  capacity, use sequential fallback.
- Wait for every profile in a group before the next group or transition. Report each completed profile once, in fixed route order, using `completed_profiles`; on resume report only
  the exact uncompleted suffix of the active group. Older clients may report each profile one at a time in fixed route order with `completed_profile`. Never advance transitions
  concurrently. Retry a failed role or run it in the parent; if it remains incomplete, stop as `partial` or `blocked`. The orchestrator validates the sequence the host reports; it
  cannot confirm that each review ran or ran concurrently. It cannot verify that host-reported `risk_signals` reflect evidence. Parallelism may increase concurrent requests/token
  use; do not change model/reasoning settings to enable it.
- `python_backend` starts all three roles together. Before dispatch, the parent must include changed behavior, relevant source/test refs, and known contracts in the packet;
  specialist reviewers cannot see the explorer result from the same group and must return evidence gaps to the parent. The parent uses the explorer map during validation/synthesis.
  Compare quality and elapsed time on representative work before applying this schedule to other bundles.
- Follow `model_policy.mode` at every stage. In `host_default`, use the host's configured model/reasoning and invent no provider/model/effort values; in `configured`, use returned
  per-stage settings. `reasoning_capabilities_verified=true` only means an exact locally curated model/provider pair; false requires host/provider verification and proves neither
  access nor capability. `qa-orch setup` configures explicit IDs; `qa-orch setup --host-default` returns to host mode. Setup does not contact provider APIs. OpenAI uses
  `reasoning.effort`; Anthropic uses `output_config.effort`. For supported OpenAI reasoning models, `none` explicitly disables reasoning; omit effort for non-reasoning models such
  as GPT-4.1. For Anthropic, `none` is a local sentinel to omit effort and use model defaults. Verify model access and custom-ID effort support with the host provider.
- Execution speed and latency follow the user's host/provider settings; the orchestrator does not set or override them.
- Keep stage labels and status text model-neutral; the stages are `Triage`, `Primary review`, optional `Deep review` and `Final synthesis`, `Host`, then `Final QA outcome`. Do not
  include model names/reasoning levels; transition identifiers are not model selectors.
- At the final primary-review group, send `completed_profiles` and `risk_signals` in the same `advance_qa_orchestration` call; send `{}` if none apply. Every true signal needs one
  concise `rationale` and one or more packet `evidence_refs`. Older clients may use `completed_profile`; the final role still sends signals. A single-profile route may set
  `skip_synthesis=true` only with no findings, unresolved material questions, or true signals; this host claim is not server-verified. Bundles, findings, material questions, or any
  true signal require normal synthesis. Do not send `risk_signals` in the later synthesis transition. The host still validates and produces the complete QA result.
- Every `advance_qa_orchestration` call needs `run_id`, `completed_step` copied from the latest session's `current_step`, and `status` (`completed` after a successful stage). Use
  the updated session returned by `advance_qa_orchestration` for the next action/model policy; use `get_qa_orchestration` only to resume or recover an interrupted/unclear session.
  If escalation is selected, run the configured deep stage once before synthesis.

## Evidence and final findings

- Inspect the full diff once and start from the authoritative task/acceptance criteria and material MR claims. Obtain only relevant code, test, observability, and runtime evidence yourself;
  check applicable repository instructions and test/CI setup before dispatch. If the requirement or a dependency contract is unavailable, record that gap rather than treating the MR description
  as its substitute. Do not pass evidence, prompts, or model outputs as content to the orchestrator; send only required references and metadata.
- Review behavior scenarios, not only changed lines. For each material requirement or claim, use the profile's `Scope` to name the expected observable result and trace the relevant path:
  entry point/trigger and inputs or state → producer and predicates/defaults/normalization → real invocation or update trigger → consumers → affected outputs and side effects. Inspect unchanged
  code only when it can change that result; keep the review within the causal path, not a repository-wide audit.
- Compare the actual rules implemented at relevant layers, including accepted inputs, transformations, defaults, and boundaries. Check a valid case plus relevant near-match negative/boundary cases.
  Compare initial/server output with client rendering and later navigation/state updates where applicable; vary scenario-relevant inputs independently, including query/state changes under the same
  identity. Check repeated or out-of-order transitions only when the contract or asynchronous flow makes them relevant.
- In `Scope`, state what evidence was checked and expected versus observed behavior. Use `Coverage Gaps` and `Unverified` for material cases or links that were not checked; distinguish source,
  test, and runtime evidence. Static inspection or a helper test does not prove that a real entry point, trigger, integration, or current value reaches the helper. Report only the dimensions actually
  exercised and name runtime/release checks not performed.
- Resolve every concrete failure hypothesis as a supported candidate, refuted with counterevidence, or unverified with its trigger and missing check; carry material unresolved scenarios into synthesis.
  Compare base and head when available and state whether a relevant defect is introduced, pre-existing, or of unknown origin. Preserve relevant pre-existing defects, especially those contradicting the
  task or MR claim; exclude unrelated defects and do not treat pre-existence alone as release severity.
- Keep one compact per-task Evidence Packet with changed behavior, verified facts, source locators, and stable `E1`, `E2`, ... refs. Give each profile only relevant sections;
  reopen only the source needed to confirm a concrete candidate or resolve a named link. Do not repeat full diffs or raw logs.
- Use stable `F-01`, `F-02`, ... candidate IDs. Each candidate cites evidence, states confidence and verification gaps, and, when supported, includes `source_locations` (`path`,
  `start_line`, optional `end_line`), `affected_area`, and concrete `impact`; profiles do not assign severity. Keep coverage gaps, evidence maps, and unverified items separate;
  deduplicate candidates during synthesis.
- Before confirming a candidate, check relevant callers/consumers, validation, error handling, and other guards. Add only needed refs; leave unchecked links/guards in the verification gap.
  Pair each new defect candidate's specific `failure_condition` with a source/contract-based `flip_condition` that would refute it. Unavailable counterevidence stays a gap; never claim an
  unperformed check or silently drop a material scenario because it is unchanged, lacks a test, or was not reproduced in a browser.
- A candidate is not a confirmed finding until the host validates it. Require evidence for both failure and material impact. Missing tests, unavailable environments, coverage gaps,
  assumptions, style preferences, and speculative worst cases are not findings; report them as open questions, coverage gaps, or residual risks. `read_only=true` and
  `host_owns_decisions=true`: the orchestrator is not an autonomous agent and owns no external writes.

### Audit-bound review mode

- For a new review with a complete diff and packet, pass `review_context` to `start_qa_orchestration`: repository identifier; distinct full base/head commit IDs; SHA-256 of exact
  diff bytes and compact packet bytes; changed paths; unique packet evidence IDs. Do not pass source contents or credentials. Omit it only for the legacy session contract.
- In a bound session, submit one `profile_reports` item for every completed profile, in the same order as `completed_profile(s)`. Each candidate has a stable local ID, title,
  description, confidence, verification gap, and registered evidence IDs. Keep findings out of `code_explorer`/`pr_test_analyzer` reports; keep gaps, unverified items, and evidence
  maps separate. Include `execution_metrics` only for accurate host-observed timestamps/token counts; the server does not verify them.
- A completed bound review requires `final_review_report` in `finish_qa_orchestration`; `profiles_reviewed` must match the route. Each final finding links its source profiles,
  original profile-local candidate IDs, and registered evidence IDs. The server checks source pairs, registered evidence, and source-location support, not truth; the host authors
  and validates the final decision.
- For every submitted profile candidate, add a host-owned `candidate_decisions` entry with source, verdict (`confirmed`, `rejected`, `duplicate`, `unverified`), rationale, and
  registered evidence refs. Confirmed/duplicate decisions link `final_finding_id`; rejected/unverified do not. Every final finding needs a confirmed source, exact source linkage,
  host-selected severity/impact/affected area, and precise source locations. Keep rejected/unverified candidates traceable.
- For candidates with failure/flip conditions, create a host-owned `refutation` assessment (`refuted`, `not_refuted`, `unverified`) with rationale/refs, bound to the exact
  candidate and full context using `make_refutation_check`. Confirmation/duplicate requires `not_refuted`, which alone does not prove a defect. Source/context changes invalidate
  it. Inspect counterevidence during existing synthesis; request another reviewer only for a named material uncertainty.
- Bound reports recover through `get_qa_orchestration` after restart only when local session storage is configured; TTL/capacity/explicit deletion also apply. Legacy reports with
  empty `source_candidates` can only be replayed as the same idempotent finalization; new reports require source candidate IDs.
- For coordinate-verifiable reviews, provide a complete `review_context.evidence_registry` and run the host-local `qa-orch evidence` checker. Register exact base/head revisions,
  repository-relative source paths, optional inclusive ranges and content hashes; bind diff entries to head/diff hash; give external evidence a sanitized locator and host hash.
  Cite a covering head-source entry for every finding location. The server validates structure/membership; the host verifies Git objects, packet bytes, and authoritative external
  evidence.

### Optional review rules and host execution

- The host may bind an explicitly approved `ReviewRuleset` as `review_context.review_rules`. Pass only applicable scoped rules to workers; evaluate every guard condition against
  evidence. Rules never suppress findings automatically. The host adapter supplies the complete digest as `review_rules_sha256` in every profile and final report; omit it without a
  bound ruleset. Rule changes require a new session.
- Per-profile checkpoints may reuse validated reports only for the same complete input and explicit executor/instruction version. Mark reused profiles separately from fresh
  execution; disable reuse for independent benchmarks. The optional Codex CLI executor reviews only the packet and uses a read-only writes sandbox; strict filesystem read isolation
  requires host OS/container controls. See `docs/REVIEW_UPGRADES.md` for configuration/limits.

### Confirmation and traceability

- Put a defect in final `Findings` only when evidence supports failure and material impact. Direct source/contract evidence can confirm a code-level defect; state when runtime,
  deployment, or release behavior was not tested.
- Do not turn a missing test, unavailable environment, coverage gap, assumption, style preference, or speculative worst case into a finding. Report these under `Open Questions /
  Could Not Verify`, `Coverage Gaps`, or `Residual Risks`.
- Every finding must have verifiable evidence. Final reports must map each `E` ID to a direct locator: repository and file/line/commit, issue/MR, test/pipeline, or log source with
  time/environment. Never leave evidence IDs unexplained. Keep evidence confidence separate from severity; uncertain candidates remain open/unverified.

### Severity, affected area, and finding language

Severity reflects evidenced impact, affected scope, consequence, and workaround—not confidence, effort, or release readiness. `Blocker`: a critical flow is unusable or unsafe (for
example, data loss/corruption or serious security/transaction-integrity failure) without reasonable mitigation. `High`: a major flow or material correctness/security failure
affects a meaningful in-scope group without reasonable workaround. `Medium`: a limited condition or user impact, or a workaround, while the core outcome remains possible. `Low`: a
minor, cosmetic, or rare edge case; core behavior and data, security, and transaction correctness remain intact. Do not raise severity for incomplete evidence or an unverified
worst case. Order findings Blocker, High, Medium, Low; localize labels without changing their meaning or order.

`Affected area` names the evidenced component, service, interface, data flow, or user scenario; for a boundary, name both sides. Add platform/role/locale only when verified and
relevant. Do not invent universal labels or use unqualified `Consumer` unless a task-specific rule defines it. Keep finding field names in English exactly as in the template; write
values in the user's language. Keep IDs, code, paths, SHAs, and exact source/UI quotes unchanged. Evidence confidence is about evidential support, not impact: `High` = direct
source/contract/test evidence supports failure and expectation; `Medium` = failure is supported but runtime/impact extent is unverified; `Low` = a key causal link, expectation, or
impact is inferred, so do not confirm it.

### Final finding format

Use this for each confirmed finding. Keep IDs stable across stages; omit a field only when genuinely inapplicable. Put each field on its own line and do not add empty boilerplate.

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

For implementation-aware reviews, list confirmed findings first, then concise change summary, targeted verification/manual-test plan, and open questions/unverified risks. If none
are confirmed, say so only for the reviewed scope; do not imply untested areas are clear.

## Optional deep analysis

- Set `risk_signals` from evidence with the final primary-review group. A true signal needs one concise `rationale` and packet `evidence_refs`; signals are host claims. Map:
  sensitive identity/security/payment/fraud/privacy/access-control impact → `high_risk_domain`; actionable unresolved conclusion a scoped read-only review could resolve →
  `evidence_uncertain`; relevant repository/service/system boundary → `cross_system_scope`; multiple plausible causes after investigation → `multiple_plausible_causes`; conflicting
  authoritative evidence → `evidence_conflict`; expected behavior not reproducible → `non_reproducible`; broad impact across consumers/data → `high_blast_radius`.
- Missing CI, Stage, or runtime checks alone, ordinary coverage gaps, and profile escalation text do not set a signal. Report unavailable checks as verification gaps; do not set
  `evidence_uncertain` for them unless they leave a specific high-impact question for deep review. Deep review runs only for: high risk plus actionable uncertainty; at least two
  complexity signals (`cross_system_scope`, `multiple_plausible_causes`, `non_reproducible`, `high_blast_radius`); or high-impact `evidence_conflict` (also `high_risk_domain`,
  `cross_system_scope`, or `high_blast_radius`). Carry the named question and refs into deep review; inspect only what can answer it. If unavailable evidence is required, mark it
  unverified and continue to synthesis. Validate `deep_assessment` and findings yourself; do not escalate a second time. Pass no raw evidence content, logs, prompts, or model
  output to the orchestrator; send only required references and review metadata.

## Finalize an orchestration

- For retry-safe transitions, send a unique `request_id` with the latest `expected_revision`; retry only the identical payload/key. Recognized retries return current retained state
  without another transition/TTL refresh and may include later progress. New stale revisions or changed payloads under the same key fail. Legacy unkeyed transitions require a state
  read after lost response. Renew before `expires_at`; renewal changes only the active lease and cannot revive expired/terminal sessions. Never replay an advance blindly: after a
  lost response/error, call `get_qa_orchestration` and continue from its latest state; start a new session if the old one is unknown or expired.
- After synthesis or a valid clean single-profile path, call `finish_qa_orchestration` once with `run_id` and host-owned `completed`, `partial`, or `blocked`. Repeating the same
  outcome is idempotent; a conflicting outcome is rejected. Do not finalize tasks that were not orchestrated. Bound completed reviews require `final_review_report`.
- The final outcome must match the `finish_qa_orchestration` call. For an early stop, use the same `partial`/`blocked` outcome sent to `advance_qa_orchestration`.
- Session state is in memory by default. With `QA_ORCHESTRATOR_SESSION_STORE_PATH`, unfinished structured state and finalized sessions with review artifacts can recover after
  restart subject to TTL/capacity; finalized legacy sessions without artifacts are removed. Reads do not refresh TTL. Use `delete_qa_orchestration(run_id)` only at the user's
  explicit request; it irreversibly deletes memory/recovery state, records no outcome, and does not stop host work. `deleted=true` confirms absence even for a missing ID; retries
  are safe. If storage deletion fails, memory state remains. Never use deletion as automatic cleanup or instead of finalization.
- Exactly eleven tools are published: `prepare_qa_orchestration`, `prepare_qa_orchestration_bundle`, `get_qa_orchestration_catalog`, `get_qa_orchestration_model_policy`,
  `start_qa_orchestration`, `advance_qa_orchestration`, `get_qa_orchestration`, `renew_qa_orchestration`, `list_qa_orchestrations`, `finish_qa_orchestration`, and
  `delete_qa_orchestration`. Use `get_qa_orchestration_catalog` with no args when route/profile purpose is unclear; it is read-only and does not change state/TTL. Triage's
  successful advance returns selected checklists/groups. Use prepare tools only for standalone lookup/recovery.
- If `run_id` is lost, use `list_qa_orchestrations` with no args, identify by task/route/stage/expiry, then call `get_qa_orchestration`. Ask which one to resume if multiple match;
  never guess or create another first. Listing includes retained terminal sessions, excludes expired/evicted ones, and does not renew TTL/write storage.
- When asked which models are loaded, call `get_qa_orchestration_model_policy` with no args. It reports current server policy for new sessions without reading disk or creating a
  session. After `qa-orch setup`, restart the server connection before checking: `config show`/`reload` read disk, not the connected process. For recovered sessions follow their
  saved `model_policy`; a later transition uses the restarted server's policy. This does not prove actual model execution; the host verifies that.
- Do not add persistent QA memory, source caches, a learning layer, or hidden tool calls.
