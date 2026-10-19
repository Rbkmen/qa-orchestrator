# Host review adapter

`qa_orchestrator.host.HostReviewAdapter` supervises an injected async profile executor. It runs the selected fixed waves, limits concurrent workers, passes checklists/context/model policy and reports from previous waves, waits for the barrier, then records one ordered group transition. It stops at deep review or synthesis: evidence gathering, risk assessment and final decisions remain host-owned.

```python
from qa_orchestrator.host import HostExecutionPolicy, HostReviewAdapter

# service already has a review-bound run with its route selected at triage.
adapter = HostReviewAdapter(service, HostExecutionPolicy(max_concurrency=2))
result = await adapter.run_primary_review(run_id, execute_profile, assess_risk)
```

`execute_profile(job)` is the host's async integration with its model/worker runtime. It receives `job.profile`, `job.route`, a session snapshot containing the bound context and model policy, `job.previous_reports`, and `job.attempt`. It must use the host's Evidence Packet and return `ProfileReviewReport`. The host must enforce the read-only worker boundary and gather provider usage; the adapter does not configure provider speed or acquire model credentials. Nothing is installed into an existing Codex/Claude session automatically.

The adapter measures UTC start/completion time around actual executor calls. Its fresh `execution_attempts` ledger retains failed, cancelled and successful calls, semaphore wait and executor duration. Token totals are available only when every attempt supplies the counter; otherwise they remain unknown. Executors can update `job.observation` before a failure so already observed usage is retained. Reused checkpoint reports remain separately identified and never become fresh attempt records. Metrics are observations of host callbacks, not proof of provider behavior or source truth. See [per-attempt observations](REVIEW_UPGRADES.md#6-per-attempt-observations).

`HostExecutionPolicy` sets concurrency, per-attempt timeout, whole-run timeout, retry count, and retry delay. Only timeouts and explicitly raised `RetryableProfileError` are retried. Permanent failures are not replayed. A failed wave is never recorded as complete; the session stops as `partial`, and `HostExecutionResult.unsaved_reports` retains successful outputs for host inspection/export. Error reason codes omit raw provider exception bodies. Budget expiry or cancellation cancels and drains worker tasks; the executor must cooperate with asyncio cancellation and clean up its own subprocesses/resources.

Continue from `result.session.current_step` with scoped deep review or host synthesis. Existing completed profiles are skipped when resuming a retained primary-review session. Partial runs keep the existing terminal semantics and require a new session to rerun the same pinned input.

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

For structured packets, the CLI executor records how many changed paths have source excerpts in the packet and how many remain in the profile's selected subset. These are host-side coverage indicators, not a completeness score: review the diff and scenario context, and report missing callers or contracts as coverage gaps. Legacy text packets do not carry these counts.

## Findings and speed

Final findings support a `blocker` severity and a `change_origin` value (`introduced`, `pre_existing`, or `unknown`). `next_step` can carry a targeted correction or verification. Render a saved structured report as Markdown with evidence IDs resolved through the bound context when available:

```sh
qa-orch report render --input final-report.json --context bound-context.json --output review.md
```

For latency, inspect `HostExecutionResult.execution_attempts`: separate semaphore wait from executor duration, and compare end-to-end wall time across identical pinned inputs. `max_concurrency` defaults to 3 and can be raised up to 32 subject to provider limits. Token totals remain unknown when any attempt omits its usage counter. Use the [benchmark protocol](BENCHMARK.md) with the same snapshot, packet, profiles, model, and reasoning before claiming a speed or quality improvement; host-reported timings do not establish provider-only inference latency.

Optional scoped repository rules, per-profile checkpoints and a read-only Codex CLI
executor are described in [Review Upgrades](REVIEW_UPGRADES.md). Checkpoint reuse
is explicit and reported separately from fresh execution; disable it for benchmarks.
