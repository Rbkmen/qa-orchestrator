# PR-Scoped Benchmark Holdout Splits

## Intent

Give QA Orchestrator a reproducible way to separate review cases used for tuning from cases reserved for evaluation. Cases from the same pull request must stay in one split so shared code and findings cannot leak across tuning and evaluation.

## Goals

- Assign whole pull-request groups deterministically to `tune` or `check` using a seed and a configurable check fraction.
- Require a stable group identity, derived from the pull-request URL when available or supplied explicitly for private cases.
- Score either split independently while keeping the digest bound to the complete dataset.
- Block a quality gate on a split-aware dataset unless the score came from `check`.
- Preserve reading and scoring behavior for existing dataset versions 1 through 3.

## Non-goals

- Invent or publish real QA findings, expected gold labels, or human adjudications.
- Replace the existing reviewer profiles or add a semantic verifier.
- Claim a recall improvement before a complete, human-adjudicated corpus is run.

## Approaches

- Manually edit split metadata in JSON: small implementation, but easy to split reviews from one pull request across both groups.
- Add a deterministic CLI command that groups by pull request and writes the assignments: selected because it makes the split repeatable and validates group consistency.
- Automatically split data during import: rejected because import should preserve its source data and callers need to review and pin the resulting split explicitly.

## Design

Add optional `split` and `split_group_id` fields to benchmark cases. The new dataset schema version 4 requires both on every case and requires an explicit source-revision status. Before splitting, a case may carry an explicit group ID without a split; the command uses that ID when present, including across different pull-request URLs, and otherwise derives a bounded group ID from the normalized pull-request URL using SHA-256. URL normalization ignores query strings, fragments, and trailing slashes. It assigns whole groups by a seeded SHA-256 ordering, targeting the requested fraction while keeping at least one group in each split.

`benchmark score --split tune|check` and `benchmark compare --split tune|check` score only cases in that split and record the selected split and dataset schema version. The full dataset digest remains the identity used by a policy. Named and experimental quality gates reject split-aware scores and speed comparisons unless the selected split is `check`. Legacy datasets and scores retain their current behavior.

## Constraints

- All cases with one explicit group ID, or with the same normalized pull-request URL when no explicit ID is supplied, must share one split and group ID.
- Splitting requires at least two identifiable pull-request groups.
- An input without source verification is marked `caller_supplied_unverified` when upgraded to schema version 4.
- Only human-judged complete gold references can support recall claims.
