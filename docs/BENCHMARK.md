# Local review benchmark

`qa-orch benchmark` scores pinned review runs using explicit adjudications from the host or a reviewer. It does not launch models or infer a match from wording, location overlap, or confidence. Local scores are development results, not official ReviewBench leaderboard scores.

## Import a public reference case

Obtain [ReviewBench](https://github.com/review-bench/ReviewBench) and fetch the case's exact base/head commits from its repository mirror. Then run:

```sh
qa-orch benchmark import-reviewbench --golden /path/to/ReviewBench/golden/CASE.json \
  --repository /path/to/mirror --output /path/to/run/dataset.json
```

The importer pins the diff SHA-256 computed by Git with `--binary --no-ext-diff --no-textconv`, preserves golden severity/category and source locations, and imports `tp` labels only. The golden-file hash records which reference was used. Keep the golden dataset outside the reviewer input; review only the pinned source, diff, and task context before opening the reference.

## Record and score a run

`BenchmarkDataset` and `BenchmarkResults` in `qa_orchestrator.benchmark` publish their Pydantic JSON schemas. A run records its case ID, exact base/head/diff and Evidence Packet hashes, model/reasoning, checklist and host-instruction hashes, elapsed milliseconds, optional token counts, candidate reports, and explicit adjudications. Every candidate must have exactly one adjudication with a rationale:

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

The output reports precision, recall against complete reference sets, missed high-impact findings, correctness by confidence tier, validated evidence/locations, time, and token counts. Missing token usage stays unknown. Aggregates stay separate by configuration and explicitly list unreviewed cases; aggregate recall is unavailable for incomplete coverage or incomplete gold.

Run matched configurations on the same cases and repetitions before comparing speed or quality. Preserve raw candidates, references and per-run scores locally outside the repository. Only aggregate, clearly labeled results should be published. See [Profile Evaluation](PROFILE_EVALUATION.md) for the benchmark protocol and Python schedule comparison.

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
Evidence Packet hashes, and reports per-case median milliseconds, median speedup
and separately scored precision/recall. `elapsed_ms` is host-reported: measure
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
