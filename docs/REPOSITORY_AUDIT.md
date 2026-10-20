# Repository profile and test audit

These commands give a bounded view of a repository beyond one merge request. They read tracked Git objects at a selected commit; they do not execute tests, contact model providers, or inspect dirty and untracked files. They do not change the repository; both commands may use the local profile cache unless `--no-cache` is used.

```bash
qa-orch repo profile --repository /path/to/repository
qa-orch repo audit-tests --repository /path/to/repository
```

Use `--revision <commit-or-ref>` to select another committed snapshot, `--output <file>` to save JSON, and `--no-cache` to skip the local profile cache. `audit-tests` accepts `--max-files` from 1 through 5000; the default is 1000. Each source blob is limited to 1 MiB and the audit reads at most 32 MiB of test sources. Skipped file counts are included in the result.

## Project profile

The profile reports tracked language counts, test-file paths, detectable test frameworks, recognized test commands in CI files and package scripts, configuration blob hashes, and the selected Git revision. Raw configuration contents and command lines are not included. Commands are normalized to known test-tool names, so environment assignments or other text on the original line are not copied into the report.

The profile cache is stored under `$QA_ORCHESTRATOR_DATA_DIR/repo-profiles/` (default: `~/.qa-orchestrator/repo-profiles/`), outside the repository. Its fingerprint includes relevant configuration blob IDs and the tracked test-path list. Ordinary source changes reuse the cached profile; a relevant configuration change or added/removed test path refreshes it. Test-file content is read again for every test audit. Cache files are written atomically with owner-only permissions where the platform supports them.

The profile leaves `critical_flows.configured` false. A repository scan cannot establish product-critical flows without a reviewed flow inventory, so this feature does not infer missing coverage for unnamed flows.

## Test-suite candidates

`audit-tests` returns candidate signals with a stable candidate ID, source path and line, and SHA-256 of the exact Git blob. Current checks cover:

- Python test functions without a direct assertion or recognized assertion call, and calls such as `now()`, `today()` and `random()`;
- fixed waits such as `sleep()`, `waitForTimeout()` and `setTimeout()` in Python or JavaScript/TypeScript;
- Python skip/xfail decorators and JavaScript/TypeScript `test.skip` / `it.skip` or focused `test.only` / `it.only`;
- exact repeated Python test bodies.

These are heuristics for follow-up, not findings or proof of flakiness. A helper can contain the assertion, a wait can be intentional, and two identical bodies may still test separate contracts. JavaScript/TypeScript assertion detection is file-level; its time/random calls and duplicate bodies are not checked yet. Python syntax that cannot be parsed produces no Python candidates; JavaScript/TypeScript source must be UTF-8 to produce candidates. `tests_identified` counts discovered declarations, not parameterized runtime cases. Other languages are inventoried but do not yet receive these assertion and flakiness checks. Output is capped at 500 candidates; `candidate_count` and `candidates_omitted` make truncation explicit.

The scanner does not calculate coverage percentages, run tests, infer critical user flows, or prove that a test is included in CI. CI command discovery is a profile hint only. Use the source hashes and locations to review each candidate before making a QA decision.
