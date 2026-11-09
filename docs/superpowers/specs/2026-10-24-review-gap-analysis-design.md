# Review Gap Analysis Design

## Intent

Improve two specific blind-review weaknesses without adding review jobs, providers, or unbounded repository scans: deleted-behavior tracing, bounded discovery of consumers that still reference removed diff anchors.

## Goals

- Make the omission auditor account for behaviorally relevant removals and replacements, their consumers, replacement path, and verification evidence.
- Search bounded pinned-repository discovery using identifiers removed by a change as well as identifiers added by it; keep omissions and limits explicit.

## Non-goals

- Add scheduled jobs, CI jobs, a second reviewer runtime, new dependencies, or external model calls.
- Infer the cause of a missed finding automatically.
- Claim a recall improvement without a controlled, adjudicated rerun.

## Design

1. Extend the omission auditor's review contract with a concise removed/replaced-behavior audit. It covers only behaviorally meaningful removals and requires evidence for each link; an absent replacement is a coverage gap until the resulting defect is independently confirmed.
2. Reuse the existing bounded relationship-term scan for deleted diff lines. Preserve separate added/removed anchor lists in the discovery result for auditability and merge both lists only for candidate retrieval.

## Verification

- Run the focused tests after each change, then the full suite, Ruff, and `git diff --check`.
