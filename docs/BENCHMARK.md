# Local review benchmark

`qa-orch benchmark` scores pinned review runs using explicit adjudications from the host or a reviewer. It does not launch models or infer a match from wording, location overlap, or confidence. Local scores are development results, not official ReviewBench leaderboard scores.

## Import a public reference case

Obtain [ReviewBench](https://github.com/review-bench/ReviewBench) and fetch the case's exact base/head commits from its repository mirror. Then run:

```sh
qa-orch benchmark import-reviewbench --golden /path/to/ReviewBench/golden/CASE.json \
  --repository /path/to/mirror --output /path/to/run/dataset.json
```

The importer pins the diff SHA-256 computed by Git with `--binary --no-ext-diff --no-textconv`, preserves golden severity/category and source locations, and imports `tp` labels only. The golden-file hash records which reference was used. Keep the golden dataset outside the reviewer input; review only the pinned source, diff, and task context before opening the reference.

## Import a pinned ReviewBench case set

The batch importer accepts a task list and matching findings map from a
ReviewBench checkout. For the official test set, use
`corpus/test/test.json` and `corpus/test/findings.json`. For a separated subset
derived from the wider corpus, use `--tasks` and set `--source-path` to a path
inside `corpus` (the default is `corpus/test`). The source URL uses the supplied
revision and path, but the importer does not verify that revision against the
checkout. Schema version 2 marks it as `caller_supplied_unverified`.
`source_sha256` binds the exact task-list and findings input bytes, not their
Git origin. Each repository mirror must already exist locally under
`<repositories-root>/<owner>_<repo>`; this command does not clone repositories
or launch reviews. For example:

When a legacy v1 dataset or results file omits the newer optional task-context
or ReviewBench metadata, serializing it keeps those absent fields omitted so
older strict consumers retain the previous JSON shape.

```sh
qa-orch benchmark import-reviewbench-set \
  --test-set /path/to/ReviewBench/corpus/test/test.json \
  --findings /path/to/ReviewBench/corpus/test/findings.json \
  --repositories-root /path/to/review-bench-mirrors \
  --source-revision FULL_REVIEWBENCH_COMMIT_SHA \
  --output /path/to/run/reviewbench-test-25.json
```

For a locally curated dev subset, pass its task list with `--tasks`, the matching
findings map, and `--source-path corpus`; keep that artifact separate from the
official test set. Every local mirror must contain the exact base and head
commits named by the task list. The importer hashes each binary diff, verifies
that every case has a matching gold record, records the caller-supplied
ReviewBench source revision and a combined SHA-256 over the two input files,
and preserves finding scope,
difficulty, and context requirements. It also retains the PR title/body as task context, pins a hash of
that context, and records the language and exact diff byte count. The host must
put the dataset's `task_context_sha256` into each `BenchmarkRun`; the scorer
rejects results from a different task prompt. The hash is SHA-256 over compact
UTF-8 JSON `{"title":"...","body":"..."}` with Unicode preserved; a missing
title or body is represented by an empty string, while a case with both fields
absent has no task-context hash. It prepares a dataset only; it does not execute
or score model runs. Keep the golden data outside reviewer inputs.

Before scheduling benchmark runs, flag cases whose raw diff alone exceeds the
executor's context budget:

```sh
qa-orch benchmark diff-budget --dataset reviewbench-dev.json \
  --max-context-bytes 1000000 --output diff-budget.json
```

`raw_diff_over_budget` is a hard blocker for a single-context run. A
`raw_diff_within_budget` result is only a lower-bound check; source excerpts,
packet serialization, and the full executor prompt still need to be checked.
The command does not launch a model.

## Record and score a run

`BenchmarkDataset` and `BenchmarkResults` in `qa_orchestrator.benchmark` publish their Pydantic JSON schemas. A run records its case ID, exact base/head/diff, task-context and Evidence Packet hashes, model/reasoning, checklist and host-instruction hashes, elapsed milliseconds, optional token counts, candidate reports, and explicit adjudications. Repeated runs and speed comparisons require identical task-context hashes for the same case. Every candidate must have exactly one adjudication with a rationale:

- `matched`: a golden ID and optional `additional_gold_ids` for explicitly adjudicated grouped defects; each gold finding receives credit once per run.
- `valid_unlisted`: source-confirmed useful finding absent from the reference.
- `false_positive`: unsupported or incorrect claim.
- `duplicate`: repeats an already matched gold finding and counts as noise.
- `unverified`: unresolved judgment; reported precision is a lower bound.

Adjudicator identity and decisions are reported data. The runner verifies structure and snapshot identity, not the correctness of the adjudication. Do not copy golden findings into reviewer output or count a reference round-trip as a model-quality run.

```sh
qa-orch benchmark score --dataset /path/to/run/dataset.json \
  --results /path/to/run/results.json --output /path/to/run/score.json
```

The output reports precision, recall against complete reference sets, missed high-impact findings, correctness by confidence tier, validated evidence/locations, time, and token counts. It also breaks gold recall down by severity, category, PR scope, difficulty, and required context so misses can guide the next prompt or evidence change. Missing token usage stays unknown. Aggregates stay separate by configuration and explicitly list unreviewed cases; overall and stratified recall are unavailable for incomplete case coverage or incomplete gold.

Apply a deterministic gate to a scored aggregate before accepting a prompt, model,
or scheduling change:

```sh
qa-orch benchmark quality-gate --score /path/to/run/score.json \
  --configuration candidate --min-cases 25 \
  --min-precision <approved-threshold> --min-recall <approved-threshold> \
  --max-missed-high-impact 0 \
  --min-evidence-validation-rate <approved-threshold> \
  --min-location-validation-rate <approved-threshold> \
  --max-mean-elapsed-ms <approved-budget> \
  --max-p95-elapsed-ms <approved-budget> \
  --min-stratum-recall <approved-threshold> \
  --min-stratum-gold-count 5 \
  --output /path/to/run/quality-gate.json
```

Precision and recall thresholds are required. Case coverage must be complete,
recall must be available, and unresolved adjudications fail the gate because they
leave precision as a lower bound. Evidence/location, mean-latency, p95-latency,
and minimum stratified-recall thresholds are optional. The stratified threshold
applies to every severity, category, scope, difficulty, or context bucket that
meets `min_stratum_gold_count`; smaller buckets are reported as insufficient
sample, and the gate fails if no bucket meets the minimum. The command returns
exit code 1 when a threshold fails, so CI can consume an approved aggregate
without launching model calls. Keep datasets,
per-run outputs and raw model text outside the repository. A passing gate still
depends on correct human/host adjudications; the scorer cannot validate them.

Run matched configurations on the same cases and repetitions before comparing speed or quality. Preserve raw candidates, references and per-run scores locally outside the repository. Only aggregate, clearly labeled results should be published. See [Profile Evaluation](PROFILE_EVALUATION.md) for the benchmark protocol and Python schedule comparison.

For a reliability score, keep the adjudicator identity fixed across repetitions
of each case as well as the model, prompt/checklist hashes, Evidence Packet, and
task context. The scorer rejects mixed adjudicators for one configuration/case
group.

To compare two host configurations, keep the model, reasoning, checklist,
instructions, review rules, Evidence Packet, adjudicator and pinned source the
same. Use the same end-to-end timing scope in every callback, then run:

```sh
qa-orch benchmark compare --dataset dataset.json --results results.json \
  --baseline sequential --candidate parallel --repetitions 3 \
  --output speed-comparison.json
```

The comparison requires every dataset case and exactly the declared number of
fresh runs per configuration. It rejects differences in model/prompt identity or
task context or Evidence Packet hashes, and reports per-case median milliseconds
and input/output tokens, paired input-token savings, median speedup, and separately
scored precision/recall. Token deltas stay unknown when usage is missing; packet
bytes are not presented as token counts. `elapsed_ms` is host-reported: measure
the same end-to-end interval (including the same preparation and finalization
work) in both callbacks. The command does not launch a model or verify that the
host actually performed fresh independent reviews; use `run_repetitions` with a
fresh-review callback, disable checkpoints, and keep gold hidden until each
attempt finishes. A speed result alone does not establish acceptable review
quality.

For fresh repeated attempts, `run_repetitions` invokes a host-supplied execution and
adjudication callback. `qa-orch benchmark reliability --dataset dataset.json
--results results.json --repetitions 3 --output reliability.json` reports stable
gold findings and quality/time spreads, with explicit incomplete repetition
coverage. See [Review Upgrades](REVIEW_UPGRADES.md) for identity and trust boundaries.
