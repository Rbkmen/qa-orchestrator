# ReviewBench Evidence Integrity Design

## Intent

Improve evidence completeness and change-scope accuracy in QA Orchestrator after the targeted four-PR ReviewBench run. The run showed substantial neighbor-search omissions and one candidate whose failure mode matched gold but whose origin label (`introduced`) conflicted with the adjudicated scope (`exacerbated-by-pr`).

## Goals

- Bind every omission reported by neighbor discovery to the review context and require the final impact ledger to preserve that exact omission set.
- Represent a defect that predates the PR but is made worse by it, and require both base-source and bound-diff evidence for that label.
- Preserve legacy contexts that do not carry an omission digest and preserve existing origin values.
- Verify the code and then rerun only the same four benchmark PRs.

## Non-goals

- Add Open Code Review as a second reviewer or copy its runtime.
- Add a new multi-agent/file-bundle execution model. Context packets already support profile selection and priorities; this benchmark does not yet establish that splitting reviews by file will improve recall.
- Change provider, CI jobs, secrets, or quality thresholds.

## Approaches

1. Adopt Open Code Review wholesale. Rejected: it adds a second review runtime and its own README describes a precision-over-recall trade-off, while our immediate gap is recall.
2. Add semantic file bundles and per-bundle agents. Deferred: current host contract has no stable relation-group identity, and the four-PR result does not isolate this as the cause of misses.
3. Harden the existing evidence contract. Selected: bind discovery omissions into the existing context and impact ledger, then add an evidence-backed `exacerbated_by_pr` origin. This closes concrete audit gaps without introducing another review engine.

## Constraints

- Keep the existing bounded discovery caps; expose their omissions faithfully instead of implying an exhaustive scan.
- `exacerbated_by_pr` requires cited head source at the finding location, base source at that same location, and the bound diff.
- Do not claim ReviewBench improvement from one run; compare the same four PRs and report adjudication uncertainty separately.
