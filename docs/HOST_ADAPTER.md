# Host review adapter

`qa_orchestrator.host.HostReviewAdapter` supervises an injected async profile executor. It runs the selected fixed waves, limits concurrent workers, passes checklists/context/model policy and reports from previous waves, waits for the barrier, then records one ordered group transition. It stops at deep review or synthesis: evidence gathering, risk assessment and final decisions remain host-owned.

```python
from qa_orchestrator.host import HostExecutionPolicy, HostReviewAdapter

# service already has a review-bound run with its route selected at triage.
adapter = HostReviewAdapter(service, HostExecutionPolicy(max_concurrency=2))
result = await adapter.run_primary_review(run_id, execute_profile, assess_risk)
```

`execute_profile(job)` is the host's async integration with its model/worker runtime. It receives `job.profile`, `job.route`, a session snapshot containing the bound context and model policy, `job.previous_reports`, and `job.attempt`. It must use the host's Evidence Packet and return `ProfileReviewReport`. The host must enforce the read-only worker boundary and gather provider usage; the adapter does not configure provider speed or acquire model credentials. Nothing is installed into an existing Codex/Claude session automatically.

The adapter measures UTC start/completion time around actual executor calls. Token counts supplied by the executor are retained for a single attempt; after retries they stay unknown because failed-attempt usage may be unavailable. `attempt_count` remains visible. Metrics are observations of host callbacks, not proof of provider behavior or source truth.

`HostExecutionPolicy` sets concurrency, per-attempt timeout, whole-run timeout, retry count, and retry delay. Only timeouts and explicitly raised `RetryableProfileError` are retried. Permanent failures are not replayed. A failed wave is never recorded as complete; the session stops as `partial`, and `HostExecutionResult.unsaved_reports` retains successful outputs for host inspection/export. Error reason codes omit raw provider exception bodies. Budget expiry or cancellation cancels and drains worker tasks; the executor must cooperate with asyncio cancellation and clean up its own subprocesses/resources.

Continue from `result.session.current_step` with scoped deep review or host synthesis. Existing completed profiles are skipped when resuming a retained primary-review session. Partial runs keep the existing terminal semantics and require a new session to rerun the same pinned input.

The adapter renews active leases during execution at `heartbeat_interval_seconds` (shortened when the remaining initial lease requires it). It submits each group with a unique request key and the revision used for that wave. Lost-response/connection retries reuse the exact payload; stale submissions cannot advance or stop a session progressed by another host. Failed lease renewal interrupts the wave, and expired sessions require a new run.
