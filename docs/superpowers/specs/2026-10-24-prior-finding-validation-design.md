# Prior Finding Validation Design

## Intent

Adapt the useful review pattern from Steph Dove's repositories: specialist
review followed by a distinct validation pass that challenges findings before
they are treated as trustworthy. Apply it to QA Orchestrator without adding a
profile, run, provider, or model call.

## Goals

- Require the existing final `OMISSION_AUDITOR` to revisit every candidate from
  prior implementation-review profiles.
- Give every candidate one explicit disposition: `supported`, `refuted`, or
  `unverified`, with a rationale and bound evidence references.
- Keep the auditor's omission and removed-behavior checks intact.
- Make clear that the disposition is model-reported evidence, not independently
  verified semantic truth by the orchestrator.

## Non-goals

- Add a new review profile or execution group.
- Change finding acceptance, merge decisions, severity, or benchmark scoring.
- Copy implementation or prompt text from source-available Klaussy projects.

## Approaches

1. Add a dedicated validator profile. Rejected because it adds a run step and
   cost, while the existing auditor already runs after the specialist reports.
2. Add validation to the final omission audit. Chosen because it reuses the
   existing final pass and prior reports without changing route topology.

## Design

The omission auditor reports a `Prior Finding Validation` table containing one
row per `(source profile, candidate ID)` pair from earlier implementation
reviews. Each row identifies the pair, disposition, evidence refs, and
rationale. It must trace a reachable trigger and consequence, check relevant
callers/consumers and controls, and include counterevidence when refuting a
candidate. Missing required evidence is `unverified`, not a confirmed finding.
If the report budget prevents exhaustive validation, every unreviewed pair must
be named as a coverage gap. The table annotates prior candidates; it does not
itself accept, delete, or rewrite them. Existing omitted-scenario and
removed-behavior checks remain separate sections.

This is a prompt-level report contract. The host and orchestrator do not
independently prove that the model performed the validation or that a semantic
disposition is correct.

## Verification

- Add a focused profile-contract test and confirm it fails before implementation.
- Run the review-profile tests, the full test suite, Ruff, and `git diff --check`.
- Do not run a provider-backed benchmark; a quality claim needs a blind,
  adjudicated comparison.
