# Host review adapter

`qa_orchestrator.host.HostReviewAdapter` supervises an injected async profile executor. It runs the selected fixed waves, limits concurrent workers, passes checklists/context/model policy and reports from previous waves, waits for the barrier, then records one ordered group transition. It stops at deep review or synthesis: evidence gathering, risk assessment and final decisions remain host-owned.

```python
from qa_orchestrator.host import HostExecutionPolicy, HostReviewAdapter

# service already has a review-bound run with its route selected at triage.
adapter = HostReviewAdapter(service, HostExecutionPolicy(max_concurrency=2))
result = await adapter.run_primary_review(run_id, execute_profile, assess_risk)
```

`execute_profile(job)` is the host's async integration with its model/worker runtime. It receives `job.profile`, `job.route`, a session snapshot containing the bound context and model policy, `job.previous_reports`, and `job.attempt`. It must use the host's Evidence Packet and return `ProfileReviewReport`. The host must enforce the read-only worker boundary and gather provider usage; the adapter does not configure provider speed or acquire model credentials. Nothing is installed into an existing Codex/Claude session automatically.

## Independent review cycle

`qa_orchestrator.review_cycle.ReviewCycle` executes a host-provided review, a separate second look focused on paths and hunks not attested by the first pass, and a separate verifier when candidates exist. Both reports and per-candidate advisory verdicts are retained; failures leave findings available and uncertain. A verifier verdict is not a final host decision. The host remains responsible for linking candidates, checking evidence and reporting any incomplete stage.

When `HostReviewAdapter.run_primary_review` receives a `CodexProfileExecutor`, it automatically runs `CodexReviewCycle` for finding-producing profiles. The evidence investigator and test analyzer retain their existing roles. Other host-provided executors are not wrapped: MCP hosts follow the canonical host instructions to run independent passes. For explicit access, `await CodexReviewCycle(executor).run(job)` returns the two reports and advisory verdicts. Each call has a separate observation; use one cycle object per job.

The combined report retains both passes and advisory verdicts. When both review passes receive the same selected packet and every necessary prompt handoff is recorded, the attempt retains each stage's receipt; these attest local delivery, not reasoning. A failed pass, incomplete verification, differing packet selections, missing receipt, or verifier references outside the selected packet leaves an explicit gap and prevents a complete input receipt. The verifier receives the bound packet; it fails closed if the input exceeds the CLI budget. The host still validates candidates and performs synthesis. The existing profile timeout applies to the entire cycle, including all model calls.

Python integrations starting an `ordinary_review` or `widget_review` should call
`service.start_qa_orchestration(task_type, context, require_review_preflight=True)`.
This applies the same context and manifest-v2 checks as the MCP endpoint. The
default remains `False` for existing Python clients: omitting the context can
create a legacy session, which does not have the strict bound-review guarantees.
The MCP endpoint always requires preflight. The host still verifies actual Git
and packet bytes; preflight checks their declared metadata, not their contents.

The adapter measures UTC start/completion time around actual executor calls. Its fresh `execution_attempts` ledger retains failed, cancelled and successful calls, semaphore wait and executor duration, bound to the review head and diff hashes. The Codex executor also records the exact prepared prompt hash, source/selected packet hashes, included refs and source paths, and whether writing the prompt to CLI stdin completed. This confirms only the local handoff attempt; it does not prove provider receipt or model reasoning. The final report shows callback outcome, context delivery, and executor-attested reviewed paths as separate facts. Token totals are available only when every attempt supplies the counter; otherwise they remain unknown. Executors can update `job.observation` before a failure so already observed usage is retained. Reused checkpoint reports remain separately identified and never become fresh attempt records. See [per-attempt observations](REVIEW_UPGRADES.md#6-per-attempt-observations).

`HostExecutionPolicy` sets concurrency, per-attempt timeout, whole-run timeout, retry count, and retry delay. Only timeouts and explicitly raised `RetryableProfileError` are retried. Permanent failures are not replayed. A failed wave is never recorded as complete; the adapter attempts to stop the session as `partial`, and `HostExecutionResult.unsaved_reports` retains successful outputs for host inspection/export. If stopping fails, the result includes a `session_revision_conflict` or `session_stop_failed` reason and attempts to read the latest session. `session_snapshot_fresh=false` means this read also failed and `session` is only the last snapshot observed by the adapter; its current status is unknown. A `transition_response_unavailable` can mean the transition was applied despite the missing response, so reconcile the latest session before reusing reports. Error reason codes omit raw provider exception bodies. Budget expiry or cancellation cancels and drains worker tasks; the executor must cooperate with asyncio cancellation and clean up its own subprocesses/resources. On cancellation, `CancelledError` carries `unsaved_reports`, `retained_outputs`, `execution_attempts`, and `session_stop_failed` for inspection.

Continue from `result.session.current_step` with scoped deep review or host synthesis. Existing completed profiles are skipped when resuming a retained primary-review session. Partial runs keep the existing terminal semantics and require a new session to rerun the same pinned input.

## Replay synthesis with fixed upstream results

To compare synthesis prompts without rerunning profile reviewers, call
`adapter.replay_synthesis(...)` after all selected profiles have completed. The
host supplies its existing synthesis executor; the adapter sends it the exact
saved context and profile reports plus the requested prompt text. The input
snapshot excludes any previous final report, so one prompt variant cannot
anchor the next. `inputs_sha256` stays the same across prompt variants when the
upstream results are identical; `prompt_sha256` and `output_sha256` identify
the prompt and report. The returned report is validated against the pinned
review context, but is not written into the session or the final decision.

```python
replay = await adapter.replay_synthesis(
    run_id,
    prompt_id="synthesis-v2",
    prompt=updated_synthesis_prompt,
    executor=execute_synthesis,
)
```

This is a host-side experiment hook, not a built-in model call. Keep the model,
reasoning, review inputs and adjudication protocol fixed, compare report quality
on a pinned benchmark, and only then decide whether to adopt a prompt change.

After host synthesis, `adapter.finish_review(...)` can submit the final report and
write its Markdown rendering to `markdown_output` in the same host flow. The
helper checks report refs and source locations against the bound context before
finalization, stages the file, finalizes through the service, then atomically
renames the artifact into place. This keeps the structured report authoritative;
the Markdown file is a rendered export. If the file rename fails after the
session is finalized, render the retained report with `qa-orch report render`.

The adapter renews active leases during execution at `heartbeat_interval_seconds` (shortened when the remaining initial lease requires it). It submits each group with a unique request key and the revision used for that wave. Lost-response/connection retries reuse the exact payload; stale submissions cannot advance or stop a session progressed by another host. Failed lease renewal interrupts the wave, and expired sessions require a new run.

An optional `evidence_verifier` callback validates the pinned input before execution. Connect it to the [local evidence checker](EVIDENCE_REGISTRY.md); a verification failure prevents worker launch. The callback receives the bound context and uses repository/packet paths supplied explicitly by the host.

## Review inputs beyond merge requests

For Git input, review identity is a repository plus an explicit base/head commit pair. A host can also bind a complete standalone MR diff without Git revisions; that establishes the diff bytes, not repository provenance or surrounding source. For Git input, the local evidence checker verifies that `changed_files` matches the pinned diff. It does not read an editor buffer or an uncommitted working tree: the host must first materialize a stable Git snapshot and bind its commit SHA.

For locally available branches or other Git refs, call `resolve_git_review_input` with the server-local repository path and explicit `base` and `head`, or run `qa-orch evidence resolve-git --repository /path/to/repo --base BASE --head HEAD`. The result supplies full commit IDs, the canonical diff hash and changed paths for the review context. It compares the two resolved commits directly; when the intended comparison starts at a merge base, resolve that base explicitly. It does not fetch missing refs, construct the evidence packet, or establish the authoritative MR diff.

To inspect code around the change, add relevant unchanged callers, consumers, contracts, configuration, or tests as source evidence at the bound base/head revision. Keep the diff and requirements mandatory. Changed-source excerpts are optional context: include the paths needed to understand the scenario, and mark lower-priority excerpts optional when the byte budget is tight. A precise finding location still requires a cited head-source excerpt for that path; the bound diff remains usable evidence without one. Record anything needed but unavailable under Coverage gaps or Unverified. The packet builder enforces the 2 MB limit but does not infer whether the host selected enough context.

For report-bound reviews, register each material acceptance criterion as one `requirement` evidence ref and each material MR behavior claim as one `claim` evidence ref. Keep refs atomic: a whole requirements document or MR description registered as one ref cannot show that every bullet was checked. Both kinds are mandatory in every profile's selected packet. `FinalReviewReport.criterion_coverage` maps each ref to expected behavior, an outcome (`confirmed`, `contradicted`, `unverified`, or `not_applicable_to_diff`), implementation refs, test/CI refs, and a rationale. The server adds an `unverified` row for any omitted ref, rejects unbound refs, and stores the resulting matrix with the final report. This is host-authored traceability; it does not prove model reasoning or the truth of an assessment.

Each `ProfileReviewReport` can record material behavior scenarios in `scenario_coverage`, with one disposition (`candidate`, `refuted`, `unverified`, or `not_applicable_to_diff`), bound `evidence_refs`, and rationale per row. References must belong to the bound packet and, when a profile selected a subset, to that subset. The final report carries the synthesized matrix. At finalization, any scenario from a profile report omitted by synthesis is preserved as `unverified`; if the matrix is full, the omitted scenario is retained as a coverage gap. This prevents synthesis from silently dropping a material scenario, but does not prove the profile inspected it.


Add `verification_records` for test, CI and runtime checks when available. Each record names the scenario, outcome, environment, source/run reference, and head SHA when known; Git input requires the reviewed head, while a diff-only record has unknown binding even if its run reports a SHA. Skipped, unrun, failed, cancelled and unknown records require a reason. Optional runtime evidence refs must point to registered `runtime` evidence with the record's head. These records are host-supplied and the orchestrator does not authenticate a CI provider or test runner. Missing records render explicitly as “no verification records supplied.”

For structured packets, the CLI executor records how many changed paths have source excerpts in the packet and how many remain in the profile's selected subset. These are host-side coverage indicators, not a completeness score: review the diff and scenario context, and report missing callers or contracts as coverage gaps. Legacy text packets do not carry these counts.

Each profile report also carries `reviewed_changed_paths`, an executor attestation listing bound changed paths it inspected, including paths with no findings. The host rejects paths outside the pinned diff and adds a `coverage_gap` for every changed path not listed. When the bound diff-hunk inventory is complete, it also adds a `coverage_gap` for every hunk index the profile did not attest. Partial inventories do not imply a known set of missing hunks. These attestations make omissions visible but cannot independently prove that the declared inspection occurred.

## Findings and speed

Final findings support a `blocker` severity and a `change_origin` value (`introduced`, `exacerbated_by_pr`, `pre_existing`, or `unknown`). `exacerbated_by_pr` requires cited base-source evidence for the same finding location and cited bound diff evidence. `next_step` can carry a targeted correction or verification. Render a saved structured report as Markdown with evidence IDs resolved through the bound context when available:

```sh
qa-orch report render --input final-report.json --context bound-context.json --output review.md
```

For latency, inspect `HostExecutionResult.execution_attempts`: separate semaphore wait from executor duration, and compare end-to-end wall time across identical pinned inputs. `max_concurrency` defaults to 3 and can be raised up to 32 subject to provider limits. Token totals remain unknown when any attempt omits its usage counter. Use the [benchmark protocol](BENCHMARK.md) with the same snapshot, packet, profiles, model, and reasoning before claiming a speed or quality improvement; host-reported timings do not establish provider-only inference latency.

Optional scoped repository rules, per-profile checkpoints and a read-only Codex CLI
executor are described in [Review Upgrades](REVIEW_UPGRADES.md). Checkpoint reuse
is explicit and reported separately from fresh execution; disable it for benchmarks.

When a CLI profile returns bounded output that fails validation or accompanies a CLI error, the adapter retains it only in memory in `HostExecutionResult.retained_outputs`, including outputs from failed retries. The bytes are excluded from repr, session storage and logs; they may contain private evidence and are untrusted. The host may inspect or revalidate them with `revalidate_retained_profile_output(retained, context)`; the same profile and evidence validation runs again. A host may explicitly supply corrected bytes using `output=`, but must review those edits. Revalidation does not certify model completion or turn the retained output into an executor receipt; no validation is bypassed. Outputs disappear when the result is discarded or the process ends.

If a combined review-cycle report exceeds its limits, `HostExecutionResult.retained_cycles` also retains the original pass reports and verifier verdicts, bound to the diff and packet hashes. These artifacts are in memory only, excluded from repr and session persistence, and must be assessed by the host. A cancelled execution exposes them on the cancellation exception. Completed passes interrupted before verification remain in `unsaved_reports`.


`advance_qa_orchestration` accepts `compact_response=true` to omit stored context, profile reports, final report, audit events, and transition receipts from that response. Workflow state, revision, model policy and triage checklists remain available. The default remains the full response; `get_qa_orchestration` always returns retained full state, including for recovery after a lost response.

For checkpoint reuse the Codex executor projects prior reports to exactly the fields it sends in the prompt, so changes only to host metrics or selection metadata do not invalidate the checkpoint. Other executors retain the full prior-report key unless they explicitly supply a projection. Use the full executor identity including the runtime version as before; a changed instruction, evidence content, route or prior finding must invalidate reuse.

The built-in review cycle caches and reuses a profile only when its complete input-handoff receipts validate. An incomplete second pass, verifier or truncated combined report is not checkpointed as a completed cycle; older cycle checkpoint keys are not reused.


When available, `failure_kind` categorizes input preparation, CLI failure, missing output, output validation, timeout, cancellation, or other executor errors. It records the observed failure stage, not an inferred provider root cause. Error text remains sanitized, and legacy records without the field remain supported.

## Multiple review inputs

For a set of MRs, maintain a host-owned JSON manifest with a unique `id` for
all in-scope inputs. Paths are relative to the manifest directory (absolute paths
also work). `prepare-batch` resolves local Git inputs and builds diff-only
packets using the existing evidence commands. It reports an independent result
for every input; a failed input does not skip the rest. Output files must not
already exist or overlap any input or another output.

```json
{"inputs": [
  {"id": "service-mr", "kind": "git", "repository": "/path/to/repo", "base": "BASE", "head": "HEAD"},
  {"id": "config-mr", "kind": "diff", "context": "context.json", "diff": "change.diff", "content": "content.json", "packet": "packet.json", "bound_context": "bound.json"}
]}
```

Run `qa-orch evidence prepare-batch --manifest batch.json`. A Git `resolved`
result contains only pinned diff metadata: construct and verify its evidence
packet separately before starting a session. A diff `ready` result is a bound
packet, subject to the same provenance and completeness requirements as
`pack-diff`. Neither status means a session started or review completed. Git
ref resolution compares commits directly, so determine the authoritative MR
comparison first. The MCP `resolve_git_review_input` is available independently
of this CLI. If `qa-orch` is absent from PATH, try the project's
`.venv/bin/qa-orch` or its installed entry point. CodeGraph failure is a
coverage gap, not by itself a reason to skip other preflight or MCP work.

Before a final response, save host-observed results for every manifest ID and
run `qa-orch evidence check-batch --manifest batch.json --results outcomes.json`:

```json
{"results": [
  {"id": "service-mr", "status": "finalized", "run_id": "qar-...", "outcome": "partial"},
  {"id": "config-mr", "status": "failed", "step": "preflight", "reason": "complete diff unavailable"}
]}
```

The command exits nonzero for missing or incomplete records. Only use `failed`
for a specific observed obstacle after feasible work; record the actual step and
reason. Verify finalized status against the session and report partial outcomes
and limitations to the user. This is a host-supplied accounting check: it cannot
verify session IDs, establish that a failure is genuine, inspect the final answer,
or force an arbitrary agent to call it. It is not an agent-behavior evaluation.

`qa-orch evidence check-agent-trace --manifest batch.json --trace trace.json`
checks normalized observations from an evaluation harness. Supply ordered events
`{"type":"start","input_id":"...","run_id":"..."}` and matching
`{"type":"finish","input_id":"...","run_id":"...","outcome":"partial"}`,
then `{"type":"final"}`. An observed obstacle uses
`{"type":"failure","input_id":"...","step":"preflight","reason":"...","observed":true}`;
allowed steps are `preflight`, `start`, `review`, and `finish`. CLI or CodeGraph
errors by themselves do not account for an MR. Events after the first final
answer do not count. Missing inputs or a missing final event return nonzero.
The harness must normalize real tool results, verify failure provenance and check
the final answer's content itself; this helper neither runs an agent nor
authenticates observations. Tests using synthetic events validate the evaluator,
not real model compliance.
