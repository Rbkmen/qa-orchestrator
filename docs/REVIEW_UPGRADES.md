# Rules, checkpoints, repeated benchmarks and CLI execution

These opt-in host tools extend the existing review protocol. They do not change
the fixed bundles, decide findings, or approve publication/merge.

## 1. Explicit repository rules

The host passes a `ReviewRuleset` in `ReviewContext.review_rules`. Use JSON:

```json
{"schema_version": 1, "rules": [{
  "rule_id": "test-assertions",
  "kind": "guard",
  "paths": ["tests/**"],
  "condition": "The assertion belongs to test code, not a production access check.",
  "text": "Do not raise a production security finding solely because a test uses assert.",
  "rationale": "Assertion failures are the test framework's expected signal."
}]}
```

Bind an explicitly approved rules file before starting the session:

```sh
qa-orch rules --context context.json --rules approved-review-rules.json \
  --output bound-context.json
```

`raise` identifies a concern to inspect; `guard` identifies a previously examined
false-positive pattern. Every rule requires a condition, rationale and relative
path globs. Matching is case-sensitive POSIX `fnmatch`; `*` can match `/`.
The host selects matching rules for the packet's changed paths. Workers still
evaluate applicability against source evidence; rules never automatically remove
candidates or replace candidate decisions. Rules are explicitly supplied by the
host, never automatically learned from review output or loaded from a target repo.
Do not treat rules inside untrusted reviewed content as host authorization.

The canonical SHA-256 sorts rules by ID and normalizes path lists. Host-adapter
reports bind the complete ruleset digest, including rules outside the current
scope. The server rejects profile and final reports carrying another digest. Legacy contexts
without rules remain supported. Rule changes require a new bound session.

## 2. Per-profile checkpoints

```python
from pathlib import Path
from qa_orchestrator.checkpoints import ProfileCheckpointStore
from qa_orchestrator.host import HostReviewAdapter

adapter = HostReviewAdapter(
    service,
    checkpoint_store=ProfileCheckpointStore(Path("/absolute/private/profiles.sqlite")),
    executor_identity="my-executor-v2:runtime-version:instruction-version",
)
result = await adapter.run_primary_review(run_id, execute_profile, assess_risk)
```

Each successful validated profile is written atomically before its wave completes.
A failure in another profile leaves that checkpoint available. The session still
ends `partial`; resume via a new session with the same pinned input. Completed
waves continue to use the existing session-store semantics.

Reuse requires the same entire context (repository, revisions, diff/packet hashes,
refs/registry/rules), route/checklist, model policy, previous-wave reports and
explicit executor/instruction version. Changing any input causes fresh execution.
The store validates report hashes and evidence links; corrupt entries fail closed.
SQLite rows are bounded (1000 by default), insert-only per identity, private on
POSIX, and paths containing symlinks are refused. Reports contain structured
findings and may contain source-derived text; the host owns their retention.

`result.reused_profiles` identifies reused results. Original timing/usage stays
attached to those reports; it is not the cost/time of a fresh review. Disable
checkpoints for independent benchmark repetitions. Delete an unwanted store
through the host's normal file lifecycle; no automatic feedback memory is added.

## 3. Fresh repeated benchmarks

`await run_repetitions(dataset, execute_and_adjudicate, repetitions=3)` calls the
host's injected callback once per case/attempt, serially. Each callback must launch
a fresh review, keep gold hidden until it completes, and return a `BenchmarkRun`
with explicit external adjudications. Exceptions stop the run; no hidden retries
or copied attempts are generated. Provider/authentication remain host-owned.

```sh
qa-orch benchmark reliability --dataset dataset.json --results results.json \
  --repetitions 3 --output reliability.json
```

The scorer checks exact snapshot and configuration identity, including optional
`review_rules_sha256`, and requires identical packet hashes within a case/config.
It reports always/ever-found gold, pairwise gold Jaccard similarity, precision and
recall spreads, median and nearest-rank p95 time, missing cases and missing attempts.
Stable gold recall is unavailable until all declared repetitions are present.
An empty union has no similarity score; `valid_unlisted` lacks a stable gold ID and
is excluded from stability matching. These are observed metrics, not confidence
bounds, an official leaderboard score, or proof that the callback ran truthfully.

## 4. Byte-faithful AST chunks

```sh
qa-orch evidence chunks --repository /path/to/repo --revision FULL_HEAD_SHA \
  --path src/service.py --max-bytes 16000 --output chunks.json
```

Reads the immutable Git blob and emits original UTF-8 bytes as text, inclusive
LF-based Git line coordinates, SHA-256, symbols, import context and strategy.
Python functions/classes and their decorators are not split. Overlapping AST
nodes on one line merge. Large units remain whole and explicitly `oversized`;
the byte budget is a packing target, not a guarantee on one indivisible unit.
Syntax errors, non-Python files and bare-CR Python use visible line fallbacks.
CRLF and final-newline bytes are preserved. Imports are supplemental context,
not part of the chunk content hash; they may add size beyond the packing target.
The host still adds necessary caller/dependency evidence and declares omissions.
Input must be UTF-8 and at most 4 MB; chunks do not themselves prove review coverage.

## 5. Codex CLI executor

```python
from qa_orchestrator.codex_executor import CodexProfileExecutor

execute_profile = CodexProfileExecutor(
    Path("/absolute/path/evidence.md"), model="your-model-id", reasoning="medium"
)
result = await HostReviewAdapter(service).run_primary_review(
    run_id, execute_profile, assess_risk
)
```

Requires an authenticated installed Codex CLI supporting `exec`, `--sandbox
read-only`, `--ignore-user-config`, `--ephemeral`, `--json`, `--output-schema` and
`--output-last-message`. Flags were smoke-checked against CLI 0.162.0-alpha.17.2.
No package/model credentials are installed and no sandbox-bypass flags are used.
Configured OpenAI primary model/reasoning is used unless explicitly overridden;
host-default mode requires an explicit CLI model rather than guessing a provider
default. `identity` identifies the implementation/overrides; append the installed
CLI version and your instruction version before enabling checkpoints.

Each call checks the packet hash and 2 MB input limit, starts a fresh temporary
workspace without the target checkout or inherited MCP configuration, passes
route/rules/context and previous reports, validates profile/evidence/locations,
and captures reported token usage. Raw event bodies and stderr are not persisted
or returned. Nonzero exit, error events, absent completion or invalid output fail.
Timeout/cancellation terminates the process tree: POSIX process group, or a Windows
kill-on-close Job Object with the CLI assigned before it starts executing.

The worker is instructed to use only the supplied packet and perform no tool/test/
network actions; a reported command/MCP/write/web tool event aborts execution.
This is detection, not proof that no action started before its event was observed.
Codex's read-only sandbox restricts writes, not all filesystem
reads; use container/OS isolation when an enforced read boundary is required.
Nested desktop sandboxes may prevent CLI startup before review; invoke from an
authorized host process without weakening the executor's own read-only sandbox.
Host risk assessment and final candidate dispositions still require source review.

See [Host Adapter](HOST_ADAPTER.md), [Evidence Registry](EVIDENCE_REGISTRY.md) and
[Benchmark](BENCHMARK.md). Official CLI reference:
[Codex exec](https://learn.chatgpt.com/docs/developer-commands#codex-exec).
