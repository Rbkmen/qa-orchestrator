# ReviewBench Evidence Integrity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep all bounded neighbor-search omissions auditable and make PR-exacerbated pre-existing findings expressible with evidence.

**Architecture:** Add a canonical digest of normalized discovery omissions to `ReviewContext`, bind it from the discovery payload, and compare it with `ImpactCoverageLedger.omissions` during validation. Extend the existing finding-origin field and enforce base-source plus diff citations for the new value.

**Tech Stack:** Python 3.12+, Pydantic, pytest.

**Spec:** `docs/superpowers/specs/2026-10-23-reviewbench-evidence-integrity-design.md`

## Global Constraints

- Preserve compatibility for contexts without the new optional omission digest.
- Preserve existing `introduced`, `pre_existing`, and `unknown` origin values.
- Do not add dependencies or an additional reviewer runtime.

## Review Focus

- Legacy discovery payload without an omission list: retain current binding behavior.
- A discovery with no omissions: bind an explicit empty omission digest.
- A discovery with non-result-limit omissions: reject an impact ledger that drops or changes them.
- An exacerbated finding with only base or only diff evidence: reject it.
- An exacerbated finding with base and diff evidence but mismatched base location: reject it.

---

### Task 1: Bind all discovery omissions

**Files:**
- Modify: `src/qa_orchestrator/contracts.py`
- Modify: `src/qa_orchestrator/evidence.py`
- Test: `tests/test_evidence.py`, `tests/test_report.py`

**Interfaces:**
- Produces: optional `ReviewContext.neighbor_discovery_omissions_sha256` and `impact_coverage_omissions_sha256(omissions)`.
- Consumes: existing `ImpactCoverageOmission` and discovery payload `omissions` list.

- [x] Add a failing test: `test_bound_discovery_omissions_must_match_impact_ledger` binds empty and non-result-limit omission sets, rejects a dropped omission, accepts the exact ledger, and preserves a legacy payload without omission metadata.
- [x] Run: `.venv/bin/pytest -q tests/test_evidence.py::test_bound_discovery_omissions_must_match_impact_ledger`; observed expected failure before the field existed, then pass after implementation.
- [x] Add the optional digest, bind normalized omissions, and compare the ledger omission digest when present.
- [x] Run: `.venv/bin/pytest -q tests/test_evidence.py tests/test_report.py tests/test_orchestration_contracts.py`; expected and observed: 181 passed.

### Task 2: Model PR-exacerbated findings

**Files:**
- Modify: `src/qa_orchestrator/contracts.py`, `src/qa_orchestrator/evidence.py`, `src/qa_orchestrator/report.py`, `src/qa_orchestrator/review_profiles.py`
- Modify: `client-rules/generic/QA_ORCHESTRATOR_INSTRUCTIONS.md`, `docs/EVIDENCE_REGISTRY.md`, `docs/HOST_ADAPTER.md`
- Test: `tests/test_evidence.py`, `tests/test_report.py`

**Interfaces:**
- Consumes: Task 1 keeps exact discovery evidence bound to the same `ReviewContext`.
- Produces: `change_origin="exacerbated_by_pr"`, requiring bound diff and base-source evidence covering each finding location.

- [x] Add a failing test: `test_schema_v2_exacerbated_origin_requires_base_and_diff_evidence` covers base-only, diff-only, wrong-location, and valid base-plus-diff cases.
- [x] Run: `.venv/bin/pytest -q tests/test_evidence.py::test_schema_v2_exacerbated_origin_requires_base_and_diff_evidence`; observed expected enum failure before implementation.
- [x] Add the origin value, evidence validation, rendered label, and reviewer guidance.
- [x] Run: `.venv/bin/pytest -q tests/test_evidence.py tests/test_report.py tests/test_review_profiles.py tests/test_install_artifacts.py`; expected and observed after updating the route contract: 151 passed.

### Task 3: Verify the targeted benchmark

**Files:**
- No production files; benchmark artifacts stay under `/private/tmp`.

- [x] Run the repository suite and Ruff; observed: 701 tests passed and Ruff reported no issues.
- [x] Rebuild and validate packets for the same four PRs, then rerun only their four-profile bundles with the same model settings; preflight passed for 4/4 and all four bundles completed (16 profile outputs).
- [x] Compare candidates with the same 33 gold findings; report confirmed, probable, missed, and discovery omissions separately. Do not claim causality from one run.
- [x] Confirm the repository has only the intended local changes; do not commit or push.

### Task 3 results

- Run artifact: `/private/tmp/qa-orch-reviewbench-live-2026-10-23/results-v46-evidence-integrity/RUN_SUMMARY.json`.
- Configuration stayed at `gpt-6.1-sol`, medium reasoning, `ordinary_mr`; the same four PRs and 33 gold findings were used. All 16 profile executions completed without retries or CLI errors.
- Manual candidate adjudication: LANdalf matched gold G-0001 (cancellation is swallowed by the generic catch); ForgeMap was a valid unlisted concurrency failure mode (a `workflow_dispatch` on `main` can cancel a push run that alone is allowed to deploy). The latter is supported by the pinned workflow and [GitHub Actions concurrency documentation](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency).
- On that manual adjudication, micro precision is 2/2 (100%), matched recall is 1/33 (3.0%), and mean per-case recall over the four selected PRs is 4.2%. This is a one-reviewer assessment, not a benchmark-wide estimate.
- Compared with the previous v43 target run, aggregate candidate precision/recall did not improve: the earlier GIMMES candidate matched gold G-0004, while the current LANdalf candidate matched G-0001; both runs also produced the same valid unlisted ForgeMap finding. The new result is a change in which defect was recovered, not a measured quality lift.
- CodeGraph was disabled for these pinned external snapshots because the available indexes did not match their revisions. Packet preflight bound and verified discovery omissions, but the run therefore does not measure CodeGraph's contribution to neighbor research.
- The full four-case run is too small and stochastic to support a causal quality claim. The implementation is justified by the evidence-integrity defect and its regression tests, not by an observed recall increase.

---

## Follow-up: improve recall through higher-signal neighbor discovery

**Observed issues:** the added-diff search missed explicit types and domain fields; matched controller ranges stopped 20 lines after the anchor; and `tests-e2e/` was not recognized as a test directory. The latter meant E2E test code could seed production searches and related E2E files were not classified as test evidence.

**Scope:** extract explicit type references and distinctive snake_case identifiers from added non-test lines; include bounded full controller blocks, paired routes, and referenced one-hop static imports; recognize qualified test directory names; and make the code-review checklist inspect changed tests as executable code. Preserve test exclusions, packet/import limits, omission reporting, and the higher priority of validators, routes, and middleware.

- [x] Add failing tests for `as Locale`, `volume_24h`, `tests-e2e`/`test-integration`, handler end boundaries, late helper calls, changed test review focus, and size limits. Each targeted regression failed before the corresponding fix and passed afterward.
- [x] Implement deterministic, bounded anchor extraction, full identifiable block-handler ranges, handler-local static-import references, test-path detection, and code-review guidance for concrete defects in changed tests.
- [x] Run focused tests, the full suite, Ruff, and diff checks. Final result: 712 passed; Ruff clean; `git diff --check` clean.
- [x] Rebuild the four packet preflights and rerun the Marvell target case with four profiles. v66 preflight passed 4/4; v67 model run completed all four profiles on the same verified dataset and gold labels.
- [x] Review the diff and preserve the existing uncommitted work. No commit or push was made.

### Targeted Marvell assessment

- v63 recovered two code gold findings (G-0002/G-0003); v64, v65, and v67 each produced one code-review candidate mapped by locations and behavior to G-0002, G-0003, and G-0004 (3/6 raw gold labels). This single-case sequence is not a causal or benchmark-wide recall result.
- v67's candidate cited the full consumer and publisher handlers and changed API methods. It still labels the issue `introduced`, while gold says `exacerbated-by-pr`; that origin difference needs adjudication. The underlying new propagation is visible in the PR diff, while the unsafe string boundary existed before it.
- The test analyzer did not emit finding candidates by design; it emitted a coverage gap that explicitly names both missing pivot-download and publisher-preview tests, matching G-0005 and G-0006 by behavior. Report these as coverage-gap matches separately from defect-finding recall.
- G-0001 assumes a remote browser. The pinned project's CI installs Chromium and runs Playwright in the same Ubuntu job, while its Playwright config uses the local browser project; no remote connection is configured in the snapshot. Official Playwright documentation says `download.path()` throws when connected remotely. Treat G-0001 as conditional or request benchmark adjudication instead of counting it as a confirmed defect for this project.
- The v66 static test audit surfaced heuristic candidates, including fixed waits in an E2E helper. They are unconfirmed signals and need review before adding them to a finding score. The changed test code and the test-gap note were present, but the model still did not report G-0001 as a defect.

### Next iteration
The v68 run on GIMMES and ForgeMap completed all four profiles but emitted zero finding candidates on both cases; LANdalf stopped in `code_explorer` after a CLI-reported failure. This is an incomplete, stochastic pilot, not a regression or recall estimate. The v66 Marvell packet contained the changed E2E test and `package.json`, but not `playwright.config.ts` or the CI workflow; all 91 refs fit every profile context with zero budget omissions. That identified a discovery gap, not a byte-budget problem.

- [x] Add bounded discovery for the nearest runner config and relevant CI workflows when changed test files are present. Require an explicit test command in workflow source, cap scanning at 64 workflow files / 8 MB, keep a 16-candidate ceiling, and report scan omissions.
- [x] Add regressions proving Playwright config and test workflows are selected while unrelated nested configs and deploy-only workflows are excluded. The focused test was observed failing before implementation and passing afterward; Ruff passed.
- [x] Rebuild a controlled Marvell packet by retaining all 75 paths from v66 and appending only `playwright.config.ts` and `.github/workflows/node-test.yml`. The pinned head and diff stayed unchanged; both additions were selected for all four profiles with zero budget omissions.
- [x] Rerun the Marvell case with all four ordinary-MR profiles. The code reviewer again produced one candidate matching G-0002/G-0003/G-0004 by behavior and location (3/6 raw labels, unchanged from v67); the explanation now cites the GET controllers and POST-only validator, with confidence high instead of medium. The test analyzer separately described the missing pivot and publisher-preview coverage for G-0005/G-0006. This one-case result does not establish a durable recall change.
- [x] Keep G-0001 conditional: pinned CI uses local Chromium and the config does not establish a remote browser, so do not count the remote-browser assumption as a confirmed defect without adjudication. The candidate's `change_origin=introduced` also differs from gold's `exacerbated-by-pr`; adjudicate that label separately.
- [x] Add host-generated coverage gaps when a complete hunk inventory contains indexes the profile did not attest. Do not infer missing hunks from a partial inventory; combine missing-path and missing-hunk details into one host note.
- [x] Verify the simulated host result against v69 reports: all four profiles attested all 5/5 hunks, so this safeguard adds no gap to the controlled run. Focused regressions observed red before implementation and green afterward.
- [x] Run the full suite, Ruff, and diff checks after implementation and docs updates: 715 passed; Ruff clean; `git diff --check` clean.
- [ ] Next: select additional pinned PRs with confirmed, non-conditional gold labels and apply the same packet construction; analyze zero-finding cases before changing prompts or thresholds. Keep code-finding recall separate from coverage-gap matches and do not claim an uplift from a single run.
