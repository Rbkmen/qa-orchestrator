# Host review adapter

`qa_orchestrator.host.HostReviewAdapter` supervises an injected async profile executor. It runs the selected fixed waves, limits concurrent workers, passes checklists/context/model policy and reports from previous waves, waits for the barrier, then records one ordered group transition. It stops at deep review or synthesis: evidence gathering, risk assessment and final decisions remain host-owned.

```python
from qa_orchestrator.host import HostExecutionPolicy, HostReviewAdapter

# service already has a review-bound run with its route selected at triage.
adapter = HostReviewAdapter(service, HostExecutionPolicy(max_concurrency=2))
result = await adapter.run_primary_review(run_id, execute_profile, assess_risk)
```

`execute_profile(job)` is the host's async integration with its model/worker runtime. It receives `job.profile`, `job.route`, a session snapshot containing the bound context and model policy, `job.previous_reports`, and `job.attempt`. It must use the host's Evidence Packet and return `ProfileReviewReport`. The host must enforce the read-only worker boundary and gather provider usage; the adapter does not configure provider speed or acquire model credentials. Nothing is installed into an existing Codex/Claude session automatically.

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

The review identity is a repository plus an explicit base/head commit pair; it is not tied to a GitLab MR. A host can review a branch, release delta, or any other commit range by binding those two commits and the exact diff. The local evidence checker also verifies that `changed_files` matches the paths in that pinned diff. It does not read an editor buffer or an uncommitted working tree: the host must first materialize a stable Git snapshot and bind its commit SHA.

To inspect code around the change, add relevant unchanged callers, consumers, contracts, configuration, or tests as source evidence at the bound base/head revision. Keep the diff and requirements mandatory. Changed-source excerpts are optional context: include the paths needed to understand the scenario, and mark lower-priority excerpts optional when the byte budget is tight. A precise finding location still requires a cited head-source excerpt for that path; the bound diff remains usable evidence without one. Record anything needed but unavailable under Coverage gaps or Unverified. The packet builder enforces the 2 MB limit but does not infer whether the host selected enough context.

For report-bound reviews, register each material acceptance criterion as one `requirement` evidence ref and each material MR behavior claim as one `claim` evidence ref. Keep refs atomic: a whole requirements document or MR description registered as one ref cannot show that every bullet was checked. Both kinds are mandatory in every profile's selected packet. `FinalReviewReport.criterion_coverage` maps each ref to expected behavior, an outcome (`confirmed`, `contradicted`, `unverified`, or `not_applicable_to_diff`), implementation refs, test/CI refs, and a rationale. The server adds an `unverified` row for any omitted ref, rejects unbound refs, and stores the resulting matrix with the final report. This is host-authored traceability; it does not prove model reasoning or the truth of an assessment.

Each `ProfileReviewReport` can record material behavior scenarios in `scenario_coverage`, with one disposition (`candidate`, `refuted`, `unverified`, or `not_applicable_to_diff`), bound `evidence_refs`, and rationale per row. References must belong to the bound packet and, when a profile selected a subset, to that subset. The final report carries the synthesized matrix. At finalization, any scenario from a profile report omitted by synthesis is preserved as `unverified`; if the matrix is full, the omitted scenario is retained as a coverage gap. This prevents synthesis from silently dropping a material scenario, but does not prove the profile inspected it.


Add `verification_records` for test, CI and runtime checks when available. Each record names the scenario, outcome, environment, source/run reference, and exact reviewed `head_sha`; skipped, unrun, failed, cancelled and unknown records require a reason. Optional runtime evidence refs must point to registered `runtime` evidence for that same head. These records are host-supplied and the orchestrator does not authenticate a CI provider or test runner. Missing records render explicitly as “no verification records supplied.”

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


`advance_qa_orchestration` accepts `compact_response=true` to omit stored context, profile reports, final report, audit events, and transition receipts from that response. Workflow state, revision, model policy and triage checklists remain available. The default remains the full response; `get_qa_orchestration` always returns retained full state, including for recovery after a lost response.

For checkpoint reuse the Codex executor projects prior reports to exactly the fields it sends in the prompt, so changes only to host metrics or selection metadata do not invalidate the checkpoint. Other executors retain the full prior-report key unless they explicitly supply a projection. Use the full executor identity including the runtime version as before; a changed instruction, evidence content, route or prior finding must invalidate reuse.


When available, `failure_kind` categorizes input preparation, CLI failure, missing output, output validation, timeout, cancellation, or other executor errors. It records the observed failure stage, not an inferred provider root cause. Error text remains sanitized, and legacy records without the field remain supported.
