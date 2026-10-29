# Public profile evaluation — 2026-10-29

This is an exploratory measurement of one profile, not a completed end-to-end
review, an approved quality gate, or an independent holdout. The public test set
has been used in earlier experiments. The user capped the run at 20 reports.

## Input and execution

- ReviewBench revision: `12e9d1aadbe7ddf4a480c6bd4ffb6ae37c7bcb67`;
  source-verified imported dataset SHA-256:
  `5428d96846e97be55e0c3ae8dd2ba317ac3808b45b53b552a7f4b4794552ee25`.
- Model: `gpt-6.1-sol`, reasoning `medium`, profile `code_reviewer`, executed
  through `HostReviewAdapter` and `CodexProfileExecutor`; Codex CLI
  `0.165.0-alpha.1.2`. These reports reached synthesis; synthesis and final
  host adjudication were not executed.
- The Git HEAD was `5670e57`; the working tree also contained uncommitted
  changes. Exact implementation and host-rule file hashes are in the
  [source manifest](2026-10-29-run-source-manifest.json).
- Context included the diff, public PR title/body, bounded changed-source
  fragments, and up to 20 discovered related files. Source fragments were
  optional under the prompt budget. Discovery, hunk inventory, packet hashes
  and actual input handoff were recorded. Gold was excluded from reviewer input.
- One large case initially exceeded the packet limit; the successful preparation
  omitted 342 changed-source files and all discovered related files. Across the
  20 cases, two omitted at least one changed-source file during preparation;
  discovery returned more candidate files than selected in 18 cases. These are
  limits on the evidence supplied, not proof that each omitted file was needed.
- Thirteen model calls in the initial 25-case queue ended with a CLI failure;
  eight were retried successfully and are included among the 20 reports. The
  other five remain without a valid report and were not retried after the user
  reduced the scope. A short model probe and a same-packet retry succeeded;
  the original errors did not contain a safe diagnostic that established the
  cause. The incomplete first attempts are retained separately from successes.

## Textual comparison with published gold

| Measure | Result |
|---|---:|
| Completed profile reports compared | 20 of 25 cases |
| Published gold findings in those cases | 280 |
| Gold descriptions matched to at least one candidate | 57 (20.4%) |
| High-severity gold in those cases | 35 |
| High-severity gold descriptions matched | 17 (48.6%) |
| Candidate findings | 57 |
| Candidates without a gold match | 17 |

A separate `gpt-6.1-terra` model matched candidate and gold descriptions in
16 cases. The primary reviewer manually matched the remaining four cases and
spot-checked selected model matches. A candidate may match multiple gold
records when it describes their specific failure mechanisms. Matching did not
independently execute or verify each defect. Therefore these are approximate
textual overlap figures, not independently adjudicated recall or precision.
The 17 unmatched candidates are not automatically false positives: they may be
valid unlisted findings, duplicates, or unsupported and remain unverified.

The five excluded cases failed in the initial execution and were left without
reports to respect the cap. Consequently no score or recall is claimed for all
25 cases; the selected 20 are not a random sample. Raw packets, reports,
matcher inputs, failure receipts and the per-case aggregation are retained in
the temporary local evaluation directory, not committed here.

The median successful executor interval was approximately 104 seconds per
report. It excludes failed earlier invocations and is not a provider-only
inference latency or a controlled speed comparison.
