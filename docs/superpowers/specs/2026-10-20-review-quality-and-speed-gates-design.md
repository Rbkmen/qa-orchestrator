# Review Quality Gate and Speed Measurement

**Status:** Draft for user review
**Goal:** Make benchmark pass/fail decisions reproducible and measure orchestration speed without accepting a quality regression.

## Current behavior

The benchmark already scores precision, recall, high-impact misses, evidence/location validation, and strata. Its quality-gate command accepts explicit thresholds and can require dataset kind and digest, but there is no approved representative corpus digest configured for an active quality gate. The speed comparison already requires identical cases and pinned inputs, pairs attempts, reports median latency and token use, and includes quality scores. It relies on callers to interleave or randomize run order and does not report p95 latency or queue wait separately.

## Design

### 1. Fail-closed quality policy

Add a named quality-gate policy that pins the exact dataset digest and dataset kind, minimum case and gold-finding coverage, precision/recall floors, zero-tolerance high-impact miss limit, and any required stratum floors. A missing digest, incomplete dataset, mismatched digest, missing configuration, or insufficient sample is “not configured” or “blocked,” never pass. Report every failed threshold and its measured value.

The implementation must not invent or approve a corpus. No active production policy is checked in until a representative curated corpus and its digest are reviewed and approved. Existing ad-hoc threshold evaluation remains available for experiments but is clearly identified as an experimental gate, not evidence of production review quality.

### 2. Speed and scheduling evidence

Extend run measurements with optional host-queue wait and a stable execution-order marker. Compare only the same cases, packets, task context, model, prompts, and adjudicator, as today. Preserve paired attempts and require a recorded interleaved or randomized schedule before treating a comparison as order-controlled. Missing queue or order data stays unknown; it is not treated as zero or as randomization.

Report per-case and aggregate median and p95 elapsed time with sample counts, queue wait separately where available, input/output token usage, and quality scores for both configurations. Mark percentile estimates with their sample count; a small run set is descriptive, not proof of a stable tail-latency improvement. Existing one-run review benchmarks remain usable for quality evaluation, but are not enough to claim a reliable p95.

Performance results never authorize an automatic increase in parallelism. A candidate configuration is eligible only if its quality evidence passes the same pinned policy as the baseline. Without an approved corpus policy, speed results remain exploratory and no quality-preservation claim is made.

### 3. Reproducibility and compatibility

New benchmark metadata is additive. Old datasets and runs remain loadable, but lack of dataset approval, order, or queue data is reported explicitly. Dataset fingerprints continue to cover pinned cases and gold labels. Benchmark execution remains opt-in; this change does not send repository or PR context to an external model service.

## Acceptance criteria

1. A production quality-gate policy cannot pass without an exact approved dataset digest and complete, adequately sized data.
2. The gate reports blocked separately from failed and passed, with reasons and threshold values.
3. A mismatch in dataset kind, digest, case coverage, configuration, or required stratum blocks a pass.
4. Speed comparison preserves same-input and paired-attempt checks and reports median/p95, sample counts, queue wait, run order, tokens, and quality without treating missing measurements as zero.
5. Order-controlled comparisons require an explicit interleaved or randomized schedule record.
6. Low-repetition p95 results are labeled descriptive and cannot satisfy a configured performance gate unless its sample minimum is met.
7. A speed improvement with quality below the pinned quality policy is rejected as an optimization candidate.
8. Legacy benchmark artifacts remain readable and are marked as missing approval or measurement metadata.
9. Tests cover digest mismatch, incomplete coverage, missing and malformed scheduling data, p95 sample size, queue metadata, unchanged paired-input checks, and quality regression.

## Boundaries

This work adds measurement and gate behavior only. It does not run a public ReviewBench corpus, transfer PR context, choose an approved dataset, tune concurrency automatically, or claim statistically conclusive results from inadequate repetitions.
