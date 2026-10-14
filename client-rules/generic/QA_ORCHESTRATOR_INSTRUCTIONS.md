# QA Orchestrator host-agent instructions

Use `qa-orchestrator` for deterministic QA routing and content-free session state. The host
agent owns evidence, analysis, candidate validation, the final QA decision, and external
actions. This file defines the generic review contract; local rules may add requirements. See
`docs/ORCHESTRATION_POLICY.md`, `docs/EVIDENCE_REGISTRY.md`, and `docs/REVIEW_UPGRADES.md` for
full protocol and optional-feature details.

## Route and run

- Use the MCP only for applicable QA work. If it is unavailable, a required call fails, or the session expires,
  follow workspace/repository rules, state that orchestration was unavailable, and report the missing evidence
  boundary. Never imitate or claim an MCP call.
- For implementation review, call `start_qa_orchestration` with exactly one fixed bundle or profile. Choose from
  changed paths and confirmed stack; recommendations are a shortlist, not a risk score. Use a profile for
  narrow, low-risk work and a bundle for broad, cross-concern, or high-risk work. Read
  `get_qa_orchestration_catalog` if the route is unclear; never invent or reorder routes.
- Use returned checklists and ordered `execution_groups`. Treat each group as a barrier: profiles may run in
  parallel when supported, but wait for all before advancing. Report the full group once, in route order, using
  `completed_profiles`; on resume report only the uncompleted suffix. Older clients may submit one
  `completed_profile` at a time. Never advance transitions concurrently.
- Temporary workers are read-only. Give each the shared Evidence Packet, relevant outputs from earlier groups,
  and only its checklist. Workers cannot call source MCPs or CodeGraph, write, set severity, own session state,
  or rely on same-group results. The parent owns evidence retrieval, validation, synthesis, and the final
  decision. Run sequentially if workers are unavailable or at capacity.
- `python_backend` runs `code_explorer`, `python_reviewer`, and `pr_test_analyzer` in one group. Put changed
  behavior, source/tests, and known contracts in the packet before dispatch; the reviewers cannot rely on the
  explorer result from that group.
- Retry a failed profile or run it in the parent. If it remains incomplete, finish as `partial` or `blocked`;
  never claim completion. The server validates the host-reported sequence, not actual execution, concurrency, or
  risk signals. Follow each checklist's focus, required sections, constraints, and escalation signals. For
  `model_policy`, use host settings in `host_default` mode and returned per-stage settings in `configured` mode.
  Do not invent settings or change them to enable parallelism; keep stage labels model-neutral.

## Evidence and findings

- Retrieve authoritative task, source, test, and runtime evidence; inspect the full diff once. Before dispatch,
  check applicable repository instructions, manifests, test configuration, and CI workflows. Build one compact
  Evidence Packet with stable `E1`, `E2`, ... references, relevant facts, and source locators. Respect
  instruction precedence. Do not load unrelated material or send source contents, raw logs, prompts, or
  credentials to the orchestrator.
- Give profiles only relevant packet sections. `code_explorer` supplies an evidence map, not defect candidates;
  `pr_test_analyzer` reports test/coverage gaps, not implementation defects. Keep gaps and unverified items
  separate from candidates.
- Give candidates stable `F-01`, `F-02`, ... IDs, evidence refs, confidence, and verification gaps; add source
  locations, affected area, and impact when supported. Before confirmation, trace callers/consumers and check
  validation, error handling, and other guards. State unchecked links explicitly. Pair a specific
  `failure_condition` with evidence-based `flip_condition` that would refute it.
- Only host-validated candidates become findings. Require evidence for both failure and material impact. Missing
  tests, unavailable CI/Stage/runtime, assumptions, style preferences, and speculative worst cases are not
  findings; report them as gaps or open questions. Keep confidence separate from severity.

## Bound review mode

- For a new review with a complete diff and packet, pass `review_context` to `start_qa_orchestration`:
  repository ID, distinct full base/head SHAs, SHA-256 of exact diff and packet bytes, changed paths, and unique
  packet evidence IDs. Do not include source contents or credentials. Omitting it uses the legacy contract.
- In bound sessions, submit one `profile_reports` entry per completed profile, in route order. Candidates need
  stable local IDs and registered evidence IDs; explorer and test-analyzer reports contain no findings. Include
  `execution_metrics` only when timestamps and token counts are accurate; they remain host-reported.
- A completed bound review requires `final_review_report` at `finish_qa_orchestration`. Match
  `profiles_reviewed` to the route. Link each final finding to source profiles, their candidate IDs, and
  registered evidence. Account for every profile candidate in `candidate_decisions` (`confirmed`, `rejected`,
  `duplicate`, `unverified`) with rationale and evidence refs. Confirmed/duplicate decisions link a final
  finding; rejected/unverified decisions do not. Each final finding needs a confirmed source, severity, impact,
  affected area, and precise locations.
- Assess refutation against the exact candidate and review context using `make_refutation_check`. Confirmation
  or duplicate linkage requires `not_refuted`, which alone does not prove a defect. Inspect counterevidence
  during synthesis; request another reviewer only for a named material uncertainty. Source/context changes
  invalidate the check.
- For coordinate-verifiable reviews, provide a complete `review_context.evidence_registry`. Run
  `qa-orch evidence` locally; see `docs/EVIDENCE_REGISTRY.md` for registry fields. Every finding
  needs a registered head-source entry. The server checks structure/membership; the host verifies
  Git objects, packet bytes, and external evidence.

## Final report

- List confirmed findings first, in `Blocker`, `High`, `Medium`, `Low` order; then give a concise change
  summary, targeted verification/manual-test plan, and open questions or unverified risks. Limit “no findings”
  to the reviewed scope. Resolve each final evidence ID to a file/line/commit, issue/MR, test/pipeline, or
  time-and-environment log locator.
- Keep the exact English field names below; write values in the user's language and localize severity/confidence
  labels without changing their meaning. `Affected area` names the evidenced component, boundary, flow, or user
  scenario. Severity describes evidenced impact and workaround; confidence describes evidence strength. Keep
  low-confidence claims unconfirmed.

```text
Finding ID: F-01
Severity: High
Affected area: API response → web checkout
Problem: <trigger, actual behavior, and violated expectation>
Evidence: E1 — <direct source locator>
Impact: <concrete consequence and affected users or flow>
Evidence confidence: <High or Medium, with a brief reason>
Verification gap: <unchecked scope, or None>
Next step: <targeted verification or correction>
```

- `Blocker`: critical in-scope flow unusable or unsafe, without reasonable mitigation. `High`: major flow or
  material correctness/security failure, without reasonable workaround. `Medium`: bounded functional failure or
  a workable workaround. `Low`: minor, narrow issue with core behavior intact. Never raise severity for an
  unverified worst case.
- Confidence is `High` when direct source/contract/test evidence supports failure and expectation; `Medium` when
  failure is supported but runtime or impact scope is unverified; `Low` when a key causal link, expectation, or
  impact is inferred. Keep low-confidence claims open, not confirmed.

## Risk escalation

- Pass `risk_signals` with the final primary-review group; use `{}` when none apply. Each true signal needs a
  concise rationale and packet `evidence_refs`. Signals are host claims. Set them only for evidenced high-impact
  domain, actionable uncertainty, cross-system scope, multiple causes, conflicting authoritative evidence,
  non-reproducibility, or broad blast radius.
- Missing CI/Stage/runtime alone, ordinary coverage gaps, and a profile's escalation text do not trigger deep
  review. Fixed rules trigger it for high risk plus actionable uncertainty, at least two complexity signals, or
  high-impact evidence conflict. Carry the named question and refs into deep review; inspect only what's needed.
  If evidence is unavailable, mark it unverified and continue to synthesis. Validate deep assessments yourself;
  do not escalate again.

## Session state and finalization

- Each `advance_qa_orchestration` call needs `run_id`, `completed_step` copied from the latest `current_step`,
  and `status` (`completed` after a successful stage). Use the returned session for the next action. Send risk
  signals with the final primary group, not synthesis. Skip synthesis only for a clean single-profile review
  with no findings, material questions, or true signals; the host still validates and finalizes the QA result.
- For retry-safe transitions, include a unique `request_id` and latest `expected_revision`; retry only the
  identical payload and key. After a lost response or unclear state, read `get_qa_orchestration` and continue
  from its latest state. Recover a lost ID with `list_qa_orchestrations`; ask if multiple sessions match. Renew
  before `expires_at` during long work. Expired sessions cannot be revived.
- After synthesis or a valid clean fast path, call `finish_qa_orchestration` with the host-owned `completed`,
  `partial`, or `blocked` outcome. Early stops must match the outcome sent to `advance_qa_orchestration`.
  Repeating the same finalization is idempotent; conflicting outcomes are rejected. Bound completed reviews
  require the final report.
- Sessions are in memory unless persistence is configured; stored unfinished sessions and finalized sessions
  with review artifacts can be recovered subject to TTL/capacity. Catalog reads do not refresh state. Delete a
  session only at the user's explicit request; deletion is irreversible, records no outcome, and does not stop
  host work.
- Optional rules are advisory and never suppress findings. With a bound `ReviewRuleset`, include
  `review_rules_sha256` in every profile and final report. Reuse checkpoints only for identical complete input
  and executor/instruction version; disable reuse for benchmarks. Do not add persistent QA memory, source
  caches, learning layers, or hidden tool calls. See `docs/REVIEW_UPGRADES.md`.
