# End-to-end quality evaluation

This protocol compares a single-reviewer baseline with the complete host-owned
orchestration workflow. It is preparation, not a quality result. A completed
evaluation requires real cases, fresh executions, independent adjudication and
an approved decision policy. Never label synthetic examples as evidence of model
quality.

## Freeze the evaluation before running

1. Select real, legally usable changes with immutable base/head revisions,
   authoritative requirements and enough source to adjudicate behavior. Include
   confirmed defects of different severities and independently reviewed changes
   with no known defect. An absence of a prior finding alone is not proof that a
   case is clean. Record missing information and exclude incomplete gold from
   recall claims.
2. Remove secrets, personal data and organization identifiers before any external
   execution. Do not claim that removing names alone makes source code anonymous;
   obtain the source owner's permission for the intended model provider, or use
   public material. Keep the mapping from anonymized IDs to original sources and
   the gold labels outside the reviewer inputs and repository.
3. Group related changes by PR, project or shared defect so that they cannot cross
   the tune/check boundary. Use `qa-orch benchmark split` with a recorded seed;
   freeze the dataset digest and assignments. A case previously used for prompt
   development or evaluation is not an independent holdout merely because it is
   placed in `check`. Retain the historical public ReviewBench cases only as a
   development smoke set unless their prior exposure has been ruled out.
4. Before viewing check results, record the intended sample size, baseline,
   candidate, repetitions, resource limits and decision thresholds. Record who
   approved the policy. Do not infer approval from a dataset hash.

## Execute and adjudicate

For each frozen case, use the same task context, exact diff and Evidence Packet
for both configurations. Record their hashes, model and reasoning settings,
instructions and checklist hashes, and the exact orchestrator revision. The
baseline is one model review; the candidate includes the full selected workflow,
host synthesis and final decisions. Run both fresh, with checkpoints disabled;
alternate or randomize their order and record the schedule. Do not silently omit
failed, timed-out, cancelled or over-budget cases. Store the attempt ledger,
elapsed time and token/cost data when available; absent usage is unknown.

Give an independent human adjudicator anonymized candidate IDs and source-bound
evidence without configuration labels. The adjudicator compares each candidate
with the separately held reference, records duplicates, unsupported claims and
unresolved cases, and checks missed reference defects. Blindness is a process
claim to record, not something the scorer can verify. Keep mappings, raw outputs
and decisions in access-controlled storage outside this repository. Disagreement
requires explicit resolution; unknown evidence remains unverified.

Use the existing `BenchmarkDataset` and `BenchmarkResults` formats and
`qa-orch benchmark score --split check` to record per-configuration outcomes.
Score both configurations against the same frozen case set. Report, by
configuration and case, confirmed findings, false positives, unresolved
findings, missed high-impact defects, incomplete/failed runs and elapsed time;
report token totals only when the attempt ledger supports them. Distinguish
conditional quality on completed runs from the completion rate over all
scheduled cases. Include sample counts and uncertainty; do not convert missing
cases into passes or calculate recall from incomplete reference sets.

`qa-orch benchmark compare` is a speed-only comparison that requires identical
model/instruction identity and rejects failures. It must not be used to claim a
single-reviewer versus full-workflow result when those instructions differ, or
to hide failed attempts. Pair quality results by case and attempt separately and
retain incomplete pairs explicitly until a comparator supporting this design is
available.

## Decision

The owner must approve thresholds before the holdout run. A suggested starting
policy for discussion is: no regression in confirmed high-impact defects,
false-positive burden or completion rate, and a predeclared maximum added
latency and cost. This is not an approved gate or a statistical guarantee.
For a small or incomplete sample, an uncertain result means collect more data,
not declare equivalence. Keep an extra pass only when the paired evidence shows
its contribution is worth its measured operational cost.
