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
inside `corpus` (the default is `corpus/test`). Without `--source-checkout`, the
source URL uses the caller-supplied revision and path, and schema version 2
marks the revision `caller_supplied_unverified`. With `--source-checkout`, the
importer requires a local Git checkout, verifies that the full SHA resolves to
a commit there, and checks that both input files are inside the selected
`--source-path` directory and byte-for-byte match that commit. Schema version 3
marks this as `checkout_verified`. `source_sha256` binds the exact task-list and
findings input bytes. Each repository mirror must already exist locally under
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
  --source-checkout /path/to/ReviewBench \
  --source-revision FULL_REVIEWBENCH_COMMIT_SHA \
  --output /path/to/run/reviewbench-test-25.json
```

For a locally curated dev subset, pass its task list with `--tasks`, the matching
findings map, and `--source-path corpus`; keep that artifact separate from the
official test set. In verified mode, `--source-path` must name a directory
inside the checkout that contains both input files. Every local mirror must
contain the exact base and head commits named by the task list. The importer
hashes each binary diff, verifies that every case has a matching gold record,
records the ReviewBench source revision and a combined SHA-256 over the two
input files, and preserves finding scope, difficulty, and context requirements.
It also retains the PR title/body as task context, pins a hash of that context,
and records the language and exact diff byte count. The host must
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

Every score includes `dataset_sha256`, a canonical SHA-256 fingerprint of the
validated dataset, including its case snapshots and adjudicated gold labels.
The explicit-threshold command below remains available for experiments. Its
output is marked `mode: experimental`; it does not establish that the dataset
or its adjudications are approved.

```sh
qa-orch benchmark quality-gate --score /path/to/run/score.json \
  --configuration candidate --min-cases 25 \
  --min-precision <approved-threshold> --min-recall <approved-threshold> \
  --max-missed-high-impact 0 \
  --require-dataset-kind curated \
  --require-dataset-sha256 "$QA_REVIEW_DATASET_SHA256"
```

The dataset digest must be reviewed and approved outside the run being gated;
the score carries the digest but does not establish who adjudicated the gold or
whether those adjudications are correct. Keep private datasets, gold labels,
per-run candidates, and adjudication details outside the repository. Synthetic
datasets remain useful for CI contract tests, but should not be accepted as the
representative quality gate.

For a reproducible gate, store a reviewed `BenchmarkQualityGatePolicy` JSON file
in trusted CI configuration and pin the exact approved dataset kind and digest.
The policy also sets minimum case and unique gold-finding coverage, precision
and recall floors, the high-impact miss limit, and any required evidence,
location, or stratum floors. A speed policy may set `min_speed_repetitions`;
without it, the speed eligibility check is blocked. Named quality policies
accept only `reviewbench_reference` or `curated` datasets; `synthetic` remains
available for contract tests and explicitly experimental threshold checks.
No policy or approved corpus digest is checked in here.

```sh
qa-orch benchmark quality-gate --score /path/to/run/score.json \
  --configuration candidate --policy /path/to/reviewed-policy.json \
  --output /path/to/run/quality-gate.json
```

Named policy output has three states. `passed` means the exact dataset identity,
coverage and measured thresholds all passed. `failed` means valid, complete
measurements fell below a quality threshold. `blocked` means the dataset
identity, case/gold coverage, attempts, or required metrics are missing,
incomplete, or inconsistent. Only `passed` returns exit code 0. A digest pins
the input data; it does not independently approve the corpus or prove the
adjudications correct. A synthetic dataset can be digest-pinned for contract
testing, but is not representative quality evidence.

For ad-hoc experiments, the explicit-threshold interface also supports additional
metrics. This output remains marked experimental:

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
exit code 1 when a threshold fails, but does not produce the named policy's
`passed`/`failed`/`blocked` state. Keep datasets,
per-run outputs and raw model text outside the repository. A passing gate still
depends on correct human/host adjudications; the scorer cannot validate them.

Run matched configurations on the same cases and repetitions before comparing speed or quality. Preserve raw candidates, references and per-run scores locally outside the repository. Only aggregate, clearly labeled results should be published. See [Profile Evaluation](PROFILE_EVALUATION.md) for the benchmark protocol and Python schedule comparison.

For a reliability score, keep the adjudicator identity fixed across repetitions
of each case as well as the model, prompt/checklist hashes, Evidence Packet, and
task context. The scorer rejects mixed adjudicators for one configuration/case
group.

To compare two host configurations, keep the model, reasoning, checklist,
instructions, review rules, Evidence Packet, adjudicator and pinned source the
same. Use the same end-to-end timing scope in every callback. A schema-v3
`BenchmarkResults` artifact may record `queue_wait_ms` and a schedule whose
`run_order` lists every successful run exactly once; failed attempts remain in
the failure ledger and are not scheduled runs. Legacy v1/v2 artifacts remain
readable when they omit these fields. Then run:

```sh
qa-orch benchmark compare --dataset dataset.json --results results.json \
  --baseline sequential --candidate parallel --repetitions 3 \
  --output speed-comparison.json
```

The comparison requires every dataset case and exactly the declared number of
fresh runs per configuration, with attempt numbers `1..N` for every
configuration/case. It pairs elapsed times by case and attempt number, rejects
differences in model/prompt identity, task context, or Evidence Packet hashes,
and reports per-case and aggregate median/p95 latency, sample counts,
queue-wait distributions, input/output tokens, and separately scored quality.
Missing queue values remain unknown and are reported with observed and missing
counts. p95 is descriptive at the reported sample count; do not treat a small
sample as stable tail-latency evidence.

An interleaved schedule is validated only when baseline and candidate alternate
in the selected run order. A randomized schedule is labeled
`randomized_host_declared`; the host's randomization is not independently
verified. Without a schedule, the assessment is `unknown`; malformed or
non-alternating interleaving is `invalid`. The comparison alone is exploratory.
To check speed eligibility against the same quality policy for baseline and
candidate, add `--quality-policy /path/to/reviewed-policy.json`. The gate blocks
unknown/invalid order, too few repetitions, incomplete measurements, or a
baseline that does not pass the policy. A candidate below the policy is failed.
Missing queue samples are reported but do not by themselves block eligibility.
The CLI returns a non-zero exit code for failed or blocked eligibility; without
the policy option it records `eligibility: exploratory` and exits successfully.

Token deltas stay unknown when usage is missing; packet bytes are not presented
as token counts. `elapsed_ms` and queue wait are host-reported: measure the same
end-to-end interval (including the same preparation and finalization work) in
both callbacks. The command does not launch a model or verify that the host
actually performed fresh independent reviews; use `run_repetitions` with a
fresh-review callback, disable checkpoints, and keep gold hidden until each
attempt finishes. A speed result alone does not establish acceptable review
quality.

For fresh repeated attempts, `run_repetitions` invokes a host-supplied execution and
adjudication callback. `qa-orch benchmark reliability --dataset dataset.json
--results results.json --repetitions 3 --output reliability.json` reports stable
gold findings and quality/time spreads, with explicit incomplete repetition
coverage. See [Review Upgrades](REVIEW_UPGRADES.md) for identity and trust boundaries.
