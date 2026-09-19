# QA Review Bundles and Named Agents Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the existing host-owned QA orchestration state machine with deterministic review bundles, user-facing specialist names, and explicit ordered profile execution while preserving the three-model policy and the six-tool MCP surface.

**Architecture:** Luna/max selects either one compatible profile or one fixed bundle. The host then invokes Terra/medium once per ordered profile, optionally asks Sol/high for a bounded read-only deep review, and uses Terra/medium for synthesis. QA Router remains a content-free contract and state service: it exposes the fixed catalog, validates transitions, returns status metadata, and never invokes models, reads evidence, or performs external writes.

**Tech Stack:** Python 3.12, FastMCP, Pydantic 2, pytest/pytest-asyncio, Ruff, in-memory TTL-bounded state, JSONL metrics.

**Spec:** `docs/superpowers/specs/2026-09-19-qa-orchestrator-design.md`

## Global Constraints

- Keep the exact model policy: `gpt-5.6-luna`/`max` for triage, `gpt-5.6-terra`/`medium` for every primary profile and synthesis, and `gpt-5.6-sol`/`high` for the optional deep read-only branch.
- Do not add model SDKs, model endpoints, local-model configuration, external agent integrations, prompt storage, evidence fields, model-output fields, or autonomous writes.
- Keep orchestration state in memory only, content-free, TTL-bounded, and limited by the existing maximum-session guard.
- Keep all existing six MCP tools and do not add a seventh tool for bundles or named agents.
- Preserve single-profile callers: a host may still select one `ReviewAgent`; bundle selection is the new deterministic path.
- Use immutable tuples and fixed enums for the bundle catalog. Do not accept a caller-provided profile list or caller-provided execution order.
- Keep `selected_profile` as a compatibility field; for a bundle it equals the first ordered profile, while `review_profiles` contains the complete ordered sequence.
- Treat Faraday as the display name of the internal `code_explorer` profile, not as a provider, model, package, or network integration.
- Existing user changes and unrelated artifacts must remain untouched.

## Review Focus

- Every bundle must resolve to the exact profile order in the approved spec; reordered, duplicated, mixed, or caller-defined lists must fail closed.
- A single profile must continue to produce the same route constraints and the same read-only/host-owned guarantees as before.
- Invalid or out-of-order transitions must not mutate the stored session or expose session content.
- An expired or unknown `run_id` must return a typed error without leaking session data.
- Deep analysis requires one fixed reason code; a reason code without a deep-analysis request is invalid.
- Structured responses and metrics must contain only metadata and fixed counters, never evidence, prompts, model outputs, or generated findings.
- Documentation must describe the host status flow and must not imply that QA Router runs an agent or model itself.

---

### Task 1: Add named review profiles and the fixed bundle catalog

**Files:**

- Modify: `src/qa_router_mcp/contracts.py`
- Modify: `src/qa_router_mcp/review_profiles.py`
- Test: `tests/test_review_profiles.py`
- Test: `tests/test_orchestration_contracts.py`

**Interfaces:**

- Add `ReviewBundle` with exactly `ordinary_mr`, `widget`, `security`, `autotest`, and `requirements` values.
- Add `display_name: str` to `ReviewRoute` and `ReviewProfileDefinition`.
- Add immutable `REVIEW_BUNDLES: Mapping[ReviewBundle, tuple[ReviewAgent, ...]]` and a helper that returns the ordered tuple for a known bundle.
- Keep the seven existing `ReviewAgent` values and their required sections, constraints, escalation signals, and host-owned read-only flags unchanged.

- [ ] **Step 1: Write failing tests for the display names and bundle order.**

  Assert the exact names:

  - `code_explorer` → `Faraday — Evidence Investigator`
  - `code_reviewer` → `Code Reviewer`
  - `pr_test_analyzer` → `Test Analyzer`
  - `security_reviewer` → `Security Reviewer`
  - `silent_failure_hunter` → `Silent Failure Hunter`
  - `typescript_reviewer` → `TypeScript Reviewer`
  - `react_reviewer` → `React Reviewer`

  Assert the exact ordered bundles:

  - `ordinary_mr`: `code_explorer`, `code_reviewer`, `pr_test_analyzer`
  - `widget`: `code_explorer`, `react_reviewer`, `typescript_reviewer`, `pr_test_analyzer`
  - `security`: `code_explorer`, `security_reviewer`, `silent_failure_hunter`
  - `autotest`: `code_reviewer`, `pr_test_analyzer`, `typescript_reviewer`
  - `requirements`: `code_explorer`, `code_reviewer`

  Also assert that the returned bundle sequence is a tuple, an unknown bundle is rejected by Pydantic, and every route remains read-only with `host_owns_decisions=True`.

- [ ] **Step 2: Run the focused tests and verify RED.**

  Run `uv run pytest -q tests/test_review_profiles.py tests/test_orchestration_contracts.py`.

  Expected result: failures identify the missing display-name field, bundle enum/catalog, and bundle assertions; unrelated existing tests must not be changed to make the failures disappear.

- [ ] **Step 3: Implement the fixed catalog and route metadata.**

  Add `ReviewBundle(StrEnum)` to `contracts.py`. Add `display_name` to the profile definitions and route model with a non-empty string constraint. Build `REVIEW_BUNDLES` from immutable tuples behind a read-only mapping and expose a small typed helper for lookup. Populate the exact names above in `REVIEW_PROFILES`, and make `build_review_route()` include the selected profile's display name.

- [ ] **Step 4: Run focused tests and lint.**

  Run `uv run pytest -q tests/test_review_profiles.py tests/test_orchestration_contracts.py` and `uv run ruff check src/qa_router_mcp/contracts.py src/qa_router_mcp/review_profiles.py tests/test_review_profiles.py tests/test_orchestration_contracts.py`.

  Expected result: all focused tests pass and Ruff reports no errors.

- [ ] **Step 5: Commit the catalog change.**

  Commit with `feat: add named qa review bundles` after checking `git diff --check`.

### Task 2: Make orchestration state bundle-aware

**Files:**

- Modify: `src/qa_router_mcp/orchestration.py`
- Modify: `tests/test_orchestration_contracts.py`
- Modify: `tests/test_orchestration.py`

**Interfaces:**

- Extend `QaOrchestrationSession` with `allowed_profiles`, `allowed_bundles`, `selected_bundle`, and ordered `review_profiles`.
- Extend `AdvanceQaOrchestrationRequest` with `selected_bundle` while retaining `selected_profile` compatibility.
- Keep the existing orchestration steps, statuses, model policy, TTL, session limit, terminal outcomes, and typed `OrchestrationError` behavior.

- [ ] **Step 1: Add failing contract tests for the selection invariant.**

  Cover these exact cases:

  - completed Luna requires exactly one of `selected_bundle` or `selected_profile`;
  - both fields together are rejected;
  - neither field is rejected for a successful Luna completion;
  - a bundle/profile selection attached to a non-Luna transition is rejected;
  - arbitrary fields such as evidence, prompt, output, or a caller-supplied profile list are rejected by `extra="forbid"`;
  - `needs_deep_analysis=True` requires one fixed `OrchestrationReason`, and a reason without deep analysis is rejected.

- [ ] **Step 2: Implement the bundle-aware Pydantic contracts.**

  Import `ReviewBundle` and the immutable catalog. Add these session fields with safe defaults:

  - `allowed_profiles: list[ReviewAgent]`
  - `allowed_bundles: list[ReviewBundle]`
  - `selected_bundle: ReviewBundle | None = None`
  - `selected_profile: ReviewAgent | None = None`
  - `review_profiles: list[ReviewAgent] = Field(default_factory=list)`

  Populate allowed values from the fixed enum/catalog at session creation. Add an after-validator to the advance request that enforces the single-selection rule and rejects selection fields on all other completed steps. Keep the existing deep-reason validation and all arbitrary-content rejection.

- [ ] **Step 3: Add failing state-machine tests for single-profile compatibility and all bundles.**

  For the existing single-profile path, assert `selected_bundle is None`, `selected_profile` is the requested profile, and `review_profiles` contains exactly that one profile.

  Parameterize over every fixed bundle. After Luna completion, assert:

  - `selected_bundle` is the requested enum;
  - `selected_profile` equals the first profile in that bundle;
  - `review_profiles` equals the exact catalog order;
  - the next policy is Terra/medium;
  - returned session copies cannot mutate the stored ordered list.

  Add a failure test that attempts to change the bundle/profile after Luna and a failure test that attempts to submit an arbitrary/reordered profile sequence.

- [ ] **Step 4: Update the state transition implementation.**

  On a completed Luna transition, resolve either the one selected profile or the fixed bundle into `review_profiles`. Set `selected_bundle` only for bundle selection and set the compatibility `selected_profile` to the first item. Keep later transitions content-free and preserve the resolved order. Do not add a transition that accepts a list from the host. Ensure all error paths validate before mutating the session map.

- [ ] **Step 5: Run the complete orchestration test slice.**

  Run `uv run pytest -q tests/test_orchestration_contracts.py tests/test_orchestration.py` and `uv run ruff check src/qa_router_mcp/orchestration.py tests/test_orchestration_contracts.py tests/test_orchestration.py`.

  Expected result: normal, Sol, terminal partial/blocked, illegal-transition, expiry, session-limit, bundle-order, and immutability tests all pass.

- [ ] **Step 6: Commit the state-machine change.**

  Commit with `feat: route qa orchestration through review bundles` after `git diff --check` passes.

### Task 3: Expose bundle selection through the existing MCP contract

**Files:**

- Modify: `src/qa_router_mcp/service.py`
- Modify: `src/qa_router_mcp/server.py`
- Modify: `tests/test_service.py`
- Modify: `tests/test_server.py`
- Test if required by the implementation: `tests/test_install_artifacts.py`

**Interfaces:**

- Keep exactly six published tools: `prepare_review_route`, `start_qa_orchestration`, `advance_qa_orchestration`, `get_qa_orchestration`, `record_qa_task_outcome`, and `get_metrics_report`.
- Add `selected_bundle: ReviewBundle | None = None` to the existing `advance_qa_orchestration` tool input and pass it into the strict request model.
- Include `display_name` in `prepare_review_route` output.
- Include fixed allowed/selected bundle and ordered profile metadata in orchestration responses without adding evidence, prompts, outputs, or findings.

- [ ] **Step 1: Add failing service and server tests.**

  Assert that a `start_qa_orchestration` response exposes all fixed bundles and profiles, starts at Luna/max, and contains no content-bearing field. Advance with `ordinary_mr` and assert the exact ordered profiles. Keep a separate test for the single-profile compatibility path and assert the route display name for `code_explorer`.

  Through the MCP boundary, reject both bundle and profile together, missing selection after successful Luna, unknown bundle values, selection on a Terra transition, and a caller-defined profile order. Preserve the exact six-tool list.

- [ ] **Step 2: Implement the minimal service/server plumbing.**

  Keep `RouterService` as the owner of the in-memory orchestrator. Add only the new typed bundle argument and response serialization needed by the contracts. Do not move model calls, evidence retrieval, or external-system access into the service or FastMCP layer.

- [ ] **Step 3: Run focused integration tests and lint.**

  Run `uv run pytest -q tests/test_service.py tests/test_server.py tests/test_install_artifacts.py` and `uv run ruff check src/qa_router_mcp/service.py src/qa_router_mcp/server.py tests/test_service.py tests/test_server.py`.

  Expected result: the six-tool surface is unchanged, bundle selection works through the public boundary, and content-bearing inputs remain rejected.

- [ ] **Step 4: Commit the MCP contract change.**

  Commit with `feat: expose review bundles through qa-router mcp` after `git diff --check` passes.

### Task 4: Update host-facing routing documentation and status flow

**Files:**

- Modify: `README.md`
- Modify: `docs/ROUTING_POLICY.md`
- Modify: `docs/clients/codex.md`
- Modify: `docs/clients/generic-mcp.md`
- Modify: `docs/clients/claude-code.md`
- Modify: `docs/clients/cursor.md`
- Modify: `client-rules/generic/QA_ROUTER_INSTRUCTIONS.md`
- Modify: `client-rules/claude-code/CLAUDE.md`
- Modify: `client-rules/cursor/qa-router.mdc`
- Modify: `tests/test_install_artifacts.py`

**Documentation contract:**

- Explain that Luna/max chooses one fixed bundle or one compatibility profile and Terra/medium executes the selected profiles in the fixed order.
- Document the five bundle names and exact ordered profiles.
- Document the seven user-facing names, including `Faraday — Evidence Investigator` for `code_explorer`.
- Show a normal host status sequence such as:

  `Luna / Max → Ordinary MR Review`

  `Terra / Medium → Faraday — Evidence Investigator`

  `Terra / Medium → Code Reviewer`

  `Terra / Medium → Test Analyzer`

  `Terra / Medium → Synthesis`

  `Host → Final QA outcome`

  Show `Sol / High → Deep read-only review` only when one fixed deep-analysis reason is present.
- State plainly that Faraday is an internal display name, not an external agent, provider, package, or model.
- Keep the six-tool MCP list and content-free boundary documentation accurate.
- Keep all retired local-model and generic-agent references absent from project documentation and client rules.

- [ ] **Step 1: Update the source-bound documents.**

  Replace statements that imply one profile is always selected with the bundle-or-profile contract. Add the exact catalog and status flow above. Preserve the current QA review responsibilities, safety rules, metrics explanation, and host-owned decision boundary.

- [ ] **Step 2: Add documentation regression assertions.**

  Assert that installation artifacts mention the five bundles, Faraday, all three model stages, and the six tools. Assert that documentation does not contain retired local-model terms, model invocation claims, evidence storage claims, or an external Faraday integration claim.

- [ ] **Step 3: Validate documentation and artifacts.**

  Run `uv run pytest -q tests/test_install_artifacts.py`, `uv run ruff check .`, and `git diff --check`. Review every changed document for consistent names, order, model/reasoning labels, and the no-external-agent boundary.

- [ ] **Step 4: Commit the documentation change.**

  Commit with `docs: document qa review bundles` after the artifact tests pass.

### Task 5: Full verification and delivery checkpoint

**Files:**

- No planned source changes; modify tests or docs only if a verification failure identifies a concrete contract mismatch.

- [ ] **Step 1: Run the full automated verification.**

  Run `uv run pytest -q`, `uv run ruff check .`, `git diff --check`, and `/bin/sh -n scripts/qa-router-mcp`.

- [ ] **Step 2: Verify the public surface and retired references.**

  Confirm the launcher still publishes exactly six tools. Search source, docs, and client rules for retired local-model terms and for accidental evidence/prompt/model-output fields. Confirm no model SDK, endpoint, or external-agent dependency was introduced.

- [ ] **Step 3: Review the final diff against the approved spec.**

  Check the exact model/reasoning matrix, all seven names, all five bundle orders, the single-profile compatibility path, immutable session copies, fail-closed transitions, and the host status sequence. Check that metrics remain aggregate and content-free.

- [ ] **Step 4: Report the evidence-backed result.**

  Report changed files, commit hashes, automated checks and their results, the unchanged six-tool surface, and any item that could not be verified. Do not claim runtime model execution, external-system access, or stage behavior because this repository intentionally does not perform those actions.

## Execution Notes

- Implement each task in order because later contracts depend on the fixed profile and bundle catalog.
- Use test-first changes inside each task: write the narrow failing test, run it to establish RED, implement the smallest change, run the focused GREEN checks, then commit.
- Keep commits separate by task so the bundle contract, state machine, MCP boundary, and documentation can be reviewed independently.
- The host remains responsible for acquiring Jira/GitLab/TestRail or other evidence, invoking the selected model with the appropriate reasoning effort, displaying the named status, and deciding the final QA outcome.
