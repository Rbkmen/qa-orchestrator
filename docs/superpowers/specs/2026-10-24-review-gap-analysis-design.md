# Review Gap Analysis Design

## Intent

Improve three specific blind-review weaknesses without adding review jobs, providers, or unbounded repository scans: deleted-behavior tracing, bounded discovery of consumers that still reference removed diff anchors, and actionable post-run benchmark miss reporting.

## Goals

- Make the omission auditor account for behaviorally relevant removals and replacements, their consumers, replacement path, and verification evidence.
- Search bounded pinned-repository discovery using identifiers removed by a change as well as identifiers added by it; keep omissions and limits explicit.
- Report each unmatched gold finding with its pinned metadata and allow a human to classify its cause as an evidence-packet gap, neighbor-tracing gap, review-analysis gap, or uncertain.
- Keep all gold comparison and miss diagnosis post-run; never include gold labels or diagnosis data in a blind review prompt.
- Preserve old benchmark result inputs and existing scoring semantics when no miss diagnosis is present.

## Non-goals

- Add scheduled jobs, CI jobs, a second reviewer runtime, new dependencies, or external model calls.
- Infer the cause of a missed finding automatically.
- Claim a recall improvement without a controlled, adjudicated rerun.

## Design

1. Extend the omission auditor's review contract with a concise removed/replaced-behavior audit. It covers only behaviorally meaningful removals and requires evidence for each link; an absent replacement is a coverage gap until the resulting defect is independently confirmed.
2. Reuse the existing bounded relationship-term scan for deleted diff lines. Preserve separate added/removed anchor lists in the discovery result for auditability and merge both lists only for candidate retrieval.
3. Extend benchmark results with optional human-authored missed-gold diagnoses. The scorer emits known unmatched gold records and attaches a diagnosis only when explicitly supplied. A new result schema version gates these annotations; older schemas remain valid. Diagnoses affect neither precision nor recall.

## Verification

- Add focused failing tests before production changes for the audit contract, deleted-anchor retrieval, unmatched-gold output, and diagnosis validation.
- Run the focused tests after each change, then the full suite, Ruff, and `git diff --check`.
- Do not run the external ReviewBench provider as part of this implementation; any later quality claim needs a fresh blind run and manual adjudication.
