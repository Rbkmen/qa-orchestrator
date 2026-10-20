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

The contract-level rejected-finding regressions in
`tests/fixtures/rejected_findings.json` cover two common mistakes: treating a
test assertion as a production authorization check, and treating a documented
empty search result as an error. The tests retain each profile candidate and
its evidence-backed `rejected` disposition, then assert that no final finding
is emitted. These examples check report handling and refutation linkage; they
are not model recall measurements.

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
not part of the chunk content hash. Repeated import context is capped at 64 KB
across the result and each chunk marks truncated context; sources producing more
than 10,000 chunks or 100,000 review units fail with an explicit error.
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

The output schema requires an empty `finding_candidates` array for
`pr_test_analyzer` and `code_explorer`, matching server-side role validation.
It also requires `reviewed_changed_paths`: list every bound path whose supplied
diff/source evidence was inspected, including paths with no finding. The host
adds a `coverage_gap` for every unlisted path and rejects paths outside the bound
change. This is an executor attestation, not independent proof of review.
Other profiles retain their existing finding limit. Executor identity v11
invalidates checkpoints produced under the previous report schema and prompt.

Each call checks the packet hash, accounts for the complete prompt envelope, and
keeps the combined prompt within 1,000,000 bytes before launch. It starts a fresh temporary
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

## 6. Per-attempt observations

`HostExecutionResult.execution_attempts` contains only fresh executor calls, in
completion order. Each record identifies its profile, attempt, outcome, UTC
interval, semaphore wait and executor duration. Report totals sum a token counter
only when that counter is known for every attempt. Unknown is never zero. Failed
and cancelled attempts remain visible; reused checkpoints have no fresh attempt
record and remain separately identified by `reused_profiles`.

Injected executors can populate `job.observation.input_tokens`, `output_tokens`,
`cached_input_tokens` and phase durations before returning or raising. The Codex
executor records preparation, its CLI/process interval (`model_seconds`, not
provider-only inference latency), and validation, including failed phases. Usage
comes from reported terminal CLI events, not estimates. A missing counter in any
turn keeps the aggregate counter unknown. No money estimate, price table, raw
exception, prompt or event body is retained.

Whole-run timeout returns the attempt ledger with a partial result. Explicit
caller cancellation still raises `asyncio.CancelledError`; after draining workers,
its `execution_attempts` attribute exposes observations already available. A
profile cancelled while waiting for a semaphore made no executor call and has no
attempt record. Timing/usage from injected callbacks remains host-reported evidence.

Fresh adapter attempt records bind each callback to the exact review head and diff.
The Codex executor records a SHA-256 of the prompt bytes, source and selected packet
hashes, included refs/source paths, and whether `stdin.drain()` completed. This
distinguishes prompt preparation, local CLI handoff, callback outcome, and the
executor's `reviewed_changed_paths` attestation. None of these values prove that a
provider received the prompt or that a model reasoned over every listed path.

## 7. Exact context packing

After building the complete evidence registry, create a packet and a newly bound
context **before** starting the review session:

```sh
qa-orch evidence pack --context registered-context.json --repository /path/to/repo \
  --plan host-context-plan.json --output context-packet.json \
  --bound-context packed-context.json
```

The optional, explicitly host-supplied plan contains `optional_profiles`,
`priorities` and `external_content`. Example:

```json
{"optional_profiles":{"E7":["pr_test_analyzer"]},
 "priorities":{"E7":20},"external_content":{"E3":"Exact requirement text"}}
```

All refs are mandatory by default. The bound diff and supplied requirements
remain mandatory. Changed-source excerpts are optional context, so a pack does
not need one source record for every changed path when the exact diff covers the
change. Any changed-source record that is supplied must use the pinned head
(base source can represent a deleted file). Hosts can make lower-priority
excerpts profile-optional; omissions are explicit, and reports cannot cite
omitted refs. A precise finding location still requires a cited head-source
excerpt for that path. The host identifies necessary callers/contracts; this
tool does not infer completeness or replace CodeGraph. External content must be
supplied explicitly and match its registered hash. Binary/non-UTF-8 material
requires a separate host evidence flow, not lossy decoding.

`ContextPacket` carries original source text, registered Git coordinates and
SHA-256. It never strips comments/docstrings, summarizes or cuts a fragment.
Selection is deterministic: mandatory first, then host priority and ref order.
Identical metadata/content can share one block while retaining every ref alias.
Optional fragments outside the profile or over budget are explicitly accounted
for. Mandatory overflow prevents process launch and requires the host to split
the review; it never becomes an empty successful review.

The CLI executor recognizes this format automatically; legacy text packets remain
supported. `max_context_bytes` (default 1,000,000) limits rendered fragment bytes,
including headers; this is a byte budget, not a tokenizer or inference budget.
The serialized input file remains limited to 2 MB. Changing the selection budget
changes executor identity. A host-owned `context_selection` binds the original
packet hash, selected text hash, included/omitted refs and byte counts. The server
rejects reports citing omitted refs or claiming to omit mandatory refs. These
assertions do not prove the model read or understood the bytes.

For structured CLI reviews, `context_selection` also reports `changed_files_count`,
`source_excerpt_files_available` and `source_excerpt_files_included`. The executor
tells the reviewer how many changed paths have source excerpts available and how
many survive profile selection. These host-derived counts expose context gaps;
they do not measure whether the excerpts are sufficient to review the scenario.

For repeated packet construction, pass the same `PinnedSourceCache` to
`build_context_packet`. It caches immutable Git blobs in memory, bounded by bytes
and 128 entries, and validates each extracted fragment against the registry.
Dirty worktree files are never read. There is no persistent learned review memory.

## 9. Neighbor evidence suggestions

Before binding a review context, scan pinned changed symbols for likely callers,
contract files and tests with:

```sh
qa-orch evidence discover --context registered-context.json \
  --repository /path/to/repo --max-candidates 50 --codegraph \
  --output neighbor-candidates.json
```

`--codegraph` adds caller and callee edges only when the local CodeGraph index is
initialized, complete, clean, and bound to the same repository checkout as the review head.
If the binary or a current index is unavailable, discovery reports that status and
continues with the pinned Git scan. It never syncs or rebuilds the index.
`codegraph.status="ready"` means the current index was queried without query
errors; it does not mean the relation scan was exhaustive. Check `truncated`,
the caller/callee query-failure counts, omitted symbols, and the `omissions` and
`relationship_coverage` sections to understand its limits.

The bounded scan reads only the pinned Git head, reports symbol matches and line
numbers, and includes source records with content hashes that can be reviewed for
the full evidence registry. Symbol search checks up to 32 deterministic,
path-related files, in batches, instead of scanning every tracked blob. A separate
module-name search uses at most 48 names from changed modules and symbols and caps
the path-list output at 32 MB; matching files are suggestions until their import
edges are checked from pinned source. Candidate symbol reads remain capped at 32
paths and 16 MB (with a per-file size limit), and final candidate source reads are
capped at twice the requested result count, up to 200. The manifest's
`relationship_coverage` separates bounded relations it traced from links it does
not resolve systematically. API and serialized contract registration, database
model/schema/migration/query semantics, string-based event subscribers, runtime
flag and authorization evaluation, background queue registration, and dynamic,
generated, runtime, or cross-repository links remain host investigation. Path
labels for API, database, event, job, config, authorization, and contract files
are heuristics, not verified integration edges. The `omissions` array gives a
reason, count, limit, and up to 20 example paths or symbols for each active bound;
CodeGraph query truncation is also reported separately.

The manifest reports these limits and scanned-file counts; `changed_source_symbol_collection_truncated` and
`changed_sources_skipped_symbol_scan` distinguish symbol-budget truncation from the
continued changed-test and direct-import scans. A missing match does not prove that a
caller is absent. CodeGraph suggestions include incoming callers and outgoing
callees for changed declarations, but remain bounded by a query/time budget and the
current local index; check its status, truncation flag, and omitted symbols. For changed Python and
JavaScript/TypeScript files, it follows local Python imports, relative or common
root-alias JS/TS imports, and common Python mock targets to matching references
and declarations. It also finds direct Python/JS/TS importers that use changed
modules, even when paths and imported names do not overlap. For changed and
selected neighbor test files, it exposes bounded assertion/wait/skip audit
candidates. Run `qa-orch repo audit-tests` with repeated `--changed-path` values
for a broader repository-wide scan; both are heuristic suggestions, not confirmed
defects. For changed test files, discovery also suggests applicable ancestor
package and test dependency manifests. It continues to suggest tests by repository
path convention. These are candidates: review and reconcile them with CodeGraph, select
the necessary paths, add their refs to the unbound context, and add their record
objects to the full records list passed to `qa-orch evidence build`. Keep every
existing record in that list because `build` replaces the registry. It does not
read the dirty worktree, change the context, or claim that discovery is complete.
Dynamic dispatch, generated code, cross-repository callers, and language-specific
dependency rules still require host investigation.

## 10. Finalize and export in one host step

After host synthesis, `HostReviewAdapter.finish_review` can validate and render
the report against the bound context, finalize the session, and atomically write
the Markdown artifact:

```python
session = adapter.finish_review(
    run_id,
    outcome="completed",
    final_review_report=final_report,
    expected_revision=current_revision,
    markdown_output=Path("artifacts/review.md"),
)
```

The export is staged before finalization and renamed into place only after the
session accepts the report. If export fails after the session finishes, the
retained structured report can be rendered again with `qa-orch report render`.

## 8. Candidate counterevidence

New review routes request paired `failure_condition` and `flip_condition` on
defect candidates. They describe the actual failure preconditions and concrete
evidence that would refute the claim. Both may be absent on legacy candidates;
half-specified or blank conditions are rejected.

For a candidate with conditions, the host must attach a `refutation` check to its
final candidate decision. Construct one only after inspecting the evidence:

```python
from qa_orchestrator.refutation import make_refutation_check

check = make_refutation_check(
    context, candidate, status="not_refuted",
    rationale="No deduplication guard precedes the side effect on the inspected path.",
    evidence_refs=("E1",),
)
```

Statuses are `refuted`, `not_refuted`, or `unverified`. They are host assessments,
not proof of an independent agent run. Confirmation/duplicate linkage requires
`not_refuted`; that status alone never confirms a defect. The existing source,
impact, lineage and decision checks still apply. If evidence is unavailable,
retain `unverified` and explain the gap. No obligatory second model pass is added.

The check hashes the complete review context and exact profile-local candidate.
Changed refs, revisions, rules, packet, coordinates or condition text invalidate
it. Finalization rejects missing, stale and unknown-ref checks. The helper does
not perform the counterevidence inspection, vote or automatically change verdicts.

These mechanisms were implemented independently; no code from the researched
third-party orchestrators was copied. Comparative latency/quality measurements
are deferred; unit tests and protocol checks are not evidence of faster reviews.

## 11. Adversarial repository content and pinned quality gates

Repository files, diffs, comments, test output, task/PR text, and prior reports
are evidence, not instructions to the reviewer. The fixed profile constraints
and Codex prompt tell workers to ignore embedded requests that override review
rules, disclose secrets, or trigger tool/network/write actions. Local executor
tests place such text inside the Evidence Packet and verify the fixed policy
and envelope; these fake-CLI tests do not measure live-model resistance. Keep
the read-only sandbox and forbidden-tool-event checks enabled, and use OS or
container isolation when an enforced filesystem boundary is required.

Benchmark scores now include a canonical SHA-256 over the validated dataset,
including source snapshots and adjudicated gold labels. Promotion gates can
require an approved `curated` or `reviewbench_reference` dataset kind and exact
dataset digest. Keep the approved digest in trusted CI configuration; do not
accept a self-declared dataset label or a synthetic fixture as representative
quality evidence. There is no approved representative dataset digest in this
repository, so no production quality gate can yet pass on a pinned curated set.

Speed comparisons require exact attempt numbers per case and configuration and
report attempt-paired latency. The host must interleave or randomize baseline
and candidate order to limit drift and provider-load bias; comparison artifacts
cannot prove the schedule that produced their timestamps.
