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

`BenchmarkDataset` and `BenchmarkResults` in `qa_orchestrator.benchmark` publish their Pydantic JSON schemas. A run records its case ID, exact head/diff hashes, model/reasoning, checklist and host-instruction hashes, elapsed milliseconds, optional token counts, candidate reports, and explicit adjudications. Every candidate must have exactly one adjudication with a rationale:

- `matched`: one golden ID; each gold finding receives credit once per run.
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
