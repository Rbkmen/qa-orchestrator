# GPT-6 Model Policy and Metrics Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move QA Orchestrator's host-facing policy to GPT-6 and add schema-v2 stage metrics while continuing to read and report existing schema-v1 history accurately.

**Architecture:** The orchestrator remains a content-free state and policy service; the host still invokes every model and owns evidence, findings, and final decisions. New outcome events use schema version 2 and stage-named counters, while the report validates and aggregates v1 and v2 separately wherever their counter meanings differ. Update the current client instructions and operational docs without rewriting dated history or the README images already changed in this checkout.

**Tech Stack:** Python 3.12, FastMCP, Pydantic 2, pytest/pytest-asyncio, Ruff, JSONL metrics.

**Spec:** `docs/superpowers/specs/2026-09-22-gpt-6-model-policy-design.md`

## Global Constraints

- Triage uses `gpt-6-luna` with `max`; primary review and synthesis use `gpt-6-sol` with `medium`; optional deep review uses `gpt-6-sol` with `high`.
- All stages retain `speed=1.0`.
- Do not add automatic fallback, a model runtime, model API calls, or a GPT-6 Astra stage.
- Keep the six-tool MCP surface unchanged.
- Keep schema-v1 events and existing JSONL rows unchanged and readable; do not rewrite or relabel history.
- V2 call fields are `triage_calls`, `primary_review_calls`, `deep_review_calls`, `synthesis_calls`, `orchestration_steps_completed`, and `orchestration_retries`.
- V2 token fields are `triage_input_tokens`, `triage_output_tokens`, `primary_review_input_tokens`, `primary_review_output_tokens`, `synthesis_input_tokens`, and `synthesis_output_tokens`; retain `deep_input_tokens` and `deep_output_tokens` for the optional deep pass.
- Preserve legacy report fields for v1 and keep legacy model counters separate from v2 stage counters; aggregate task totals across both schemas and retain exact deep model IDs.
- Keep the orchestration state content-free and host-owned; do not change bundles, profile order, escalation thresholds, or external-write ownership.
- Work in the user's current `main` checkout. Do not create a worktree, commit, or push without a separate explicit request.
- Preserve the current README image links and both new image assets; do not rewrite dated 2026-09-19 design/plan documents.

## Review Focus

- A mixed v1/v2 JSONL history must remain readable without changing old model attribution; test one report containing v1 `gpt-5.6-sol` and v2 `gpt-6-sol` events.
- An unsupported schema version, a v1 event carrying v2-only fields, or a v2 event carrying legacy model-family counters must fail validation; test each boundary, including `True` as an invalid integer version.
- A v2 deep-review event must require the selected deep branch, a positive deep-review call count, and exactly `gpt-6-sol`/`high`; test missing, extra-branch, and wrong-model metadata.
- Partial or blocked orchestrated outcomes may stop before synthesis; test that they are accepted with valid nonnegative stage counts and do not inherit completed-flow minimums.
- Non-orchestrated v2 events may omit stage-token measurements, but nonzero stage-call counters without orchestration must be rejected and incomplete token measurements must not be counted as complete.

---

## File Map

- `src/qa_orchestrator/orchestration.py` owns model IDs, stage policy, and next-action wording.
- `src/qa_orchestrator/events.py` owns schema-versioned event fields, validation, and JSONL serialization.
- `src/qa_orchestrator/service.py` owns metric input validation against the live orchestration session.
- `src/qa_orchestrator/server.py` exposes the typed metric input through the existing MCP tool.
- `src/qa_orchestrator/report.py` owns v1/v2 reading, aggregation, and the strict report response shape.
- `tests/test_orchestration_contracts.py`, `tests/test_service.py`, `tests/test_server.py`, `tests/test_events.py`, and `tests/test_report.py` pin the public and internal contracts.
- `README.md`, `docs/ORCHESTRATION_POLICY.md`, current files in `docs/clients/`, and the three `client-rules/` files are the current host-facing policy sources; `tests/test_install_artifacts.py` checks their consistency.

## Interfaces

- `OrchestrationModel` exposes only `LUNA = "gpt-6-luna"` and `SOL = "gpt-6-sol"`. Keep existing `OrchestrationStep` values as stable transition identifiers; hosts must select the model from the returned `model_policy`, not infer it from a historical step label.
- `OrchestratorService.record_qa_task_outcome(...)` and the existing MCP `record_qa_task_outcome(...)` accept the four v2 call counters, six v2 stage-token fields, and the existing common/deep/scope fields. The service assigns `schema_version=2`; clients do not choose a schema version.
- `valid_qa_task_metrics(event: dict[str, object]) -> bool` accepts v1 events (missing version defaults to v1 for internal legacy validation) and explicitly versioned v2 events, rejects unsupported versions and cross-version model counters, and applies version-specific invariants.
- `summarize_events(lines: Iterable[str], days: int = 7) -> dict[str, object]` reports v1 model-family totals under the existing keys and v2 stage totals under new `stage_calls` and `stage_tokens` keys. `MetricsReport` validates both sets of keys.

### Task 1: Switch orchestration policy to GPT-6

**Files:**

- Modify: `src/qa_orchestrator/orchestration.py`
- Test: `tests/test_orchestration_contracts.py`
- Test: `tests/test_orchestration.py`
- Test: `tests/test_service.py`

**Interfaces:**

- Keep the current four `OrchestrationStep` transition values and the state-machine order unchanged.
- Set `LUNA_TRIAGE` to `gpt-6-luna`/`max`, `TERRA_PRIMARY_REVIEW` and `TERRA_SYNTHESIS` to `gpt-6-sol`/`medium`, and `SOL_DEEP_REVIEW` to `gpt-6-sol`/`high`.
- Preserve `ModelPolicy.speed` as `Literal[1.0]`; do not add another model enum member or model-selection input.

- [x] **Step 1: Pin the complete new model mapping in the orchestration contract test.**

  Replace the body of the existing `test_model_policy_assigns_requested_models_and_reasoning` with this parameterized version; assert the four step identifiers remain unchanged. Keep `test_model_policy_pins_unit_speed` as the separate immutable-speed test.

```python
@pytest.mark.parametrize(
    ("step", "model", "reasoning"),
    [
        (OrchestrationStep.LUNA_TRIAGE, "gpt-6-luna", "max"),
        (OrchestrationStep.TERRA_PRIMARY_REVIEW, "gpt-6-sol", "medium"),
        (OrchestrationStep.SOL_DEEP_REVIEW, "gpt-6-sol", "high"),
        (OrchestrationStep.TERRA_SYNTHESIS, "gpt-6-sol", "medium"),
    ],
)
def test_model_policy_assigns_requested_models_and_reasoning(step, model, reasoning):
    policy = MODEL_POLICIES[step]
    assert policy.model.value == model
    assert policy.reasoning == reasoning
    assert policy.speed == 1.0
    assert set(MODEL_POLICIES) == {
        OrchestrationStep.LUNA_TRIAGE,
        OrchestrationStep.TERRA_PRIMARY_REVIEW,
        OrchestrationStep.SOL_DEEP_REVIEW,
        OrchestrationStep.TERRA_SYNTHESIS,
    }
```

- [x] **Step 2: Run the policy tests and verify RED.**

  Run: `PYTHONPATH=src .venv/bin/pytest -q tests/test_orchestration_contracts.py::test_model_policy_assigns_requested_models_and_reasoning tests/test_orchestration_contracts.py::test_model_policy_pins_unit_speed`

  Expected: the four new model-ID assertions fail against the current GPT-5.6 policy; the speed invariant remains covered.

- [x] **Step 3: Change only the enum values and policy assignments.**

```python
class OrchestrationModel(StrEnum):
    LUNA = "gpt-6-luna"
    SOL = "gpt-6-sol"
```

  Use `OrchestrationModel.SOL` for primary review, deep review, and synthesis; update the policy table and `_NEXT_ACTIONS` wording to say Sol for primary review and synthesis. Keep the `OrchestrationStep` wire values unchanged, preserve deterministic transitions, and do not add a compatibility fallback. Replace remaining `OrchestrationModel.TERRA` references in `tests/test_orchestration_contracts.py` with `OrchestrationModel.SOL`, including the fixed-speed rejection case. Update all current-policy model expectations and valid deep-model inputs in `tests/test_orchestration.py` and `tests/test_service.py` to the GPT-6 mapping; retain old IDs only in v1 history fixtures. Rename `test_deep_flow_uses_sol_then_returns_to_terra` to `test_deep_flow_uses_sol_then_returns_to_sol_synthesis`. Update `tests/test_service.py::test_service_exposes_fixed_profiles_and_bundles` to expect `gpt-6-luna`.

- [x] **Step 4: Run the orchestration slice and Ruff.**

  Run: `PYTHONPATH=src .venv/bin/pytest -q tests/test_orchestration_contracts.py tests/test_orchestration.py tests/test_service.py::test_service_exposes_fixed_profiles_and_bundles`

  Run: `PYTHONPATH=src .venv/bin/ruff check src/qa_orchestrator/orchestration.py tests/test_orchestration_contracts.py tests/test_orchestration.py tests/test_service.py`

  Expected: all model-policy, transition, deep-escalation, and existing state-machine tests pass; the service's new-session policy reports GPT-6; the step IDs and six-tool surface are unchanged. Deep metric integration tests are verified with Task 2 after v2 validation and input are implemented.

### Task 2: Add v2 metric input, event validation, and serialization

**Files:**

- Modify: `src/qa_orchestrator/events.py`
- Modify: `src/qa_orchestrator/service.py`
- Modify: `src/qa_orchestrator/server.py`
- Test: `tests/test_events.py`
- Test: `tests/test_service.py`
- Test: `tests/test_server.py`

**Interfaces:**

- Keep v1 `luna_calls`, `terra_calls`, `sol_calls`, and model-token fields valid only under schema version 1. V2 uses stage counters plus the shared completed-step/retry counters.
- V2 orchestration requires all four stage call fields and both shared orchestration fields; completed sessions with `N` profiles require at least one triage, `N` primary-review, and one synthesis call, and at least `N + 2` completed steps.
- On an orchestrated event, a selected deep branch requires a positive `deep_review_calls`, `gpt-6-sol`, and `high`; the non-deep branch requires `deep_review_calls == 0`. Non-orchestrated events keep orchestration-stage call counters at zero. Keep `deep_input_tokens` and `deep_output_tokens` as deep-stage token measurements.
- The MCP tool count remains six; the host supplies counters but cannot select the schema version.

- [x] **Step 1: Add a valid v2 event and boundary tests before changing validation.**

```python
def test_v2_completed_orchestration_accepts_stage_counters():
    event = {
        "schema_version": 2,
        "task_type": "ordinary_review",
        "outcome": "completed",
        "deep_analysis_used": False,
        "orchestration_used": True,
        "codegraph_calls": 0,
        "source_mcp_calls": 0,
        "findings_identified": 0,
        "findings_confirmed": 0,
        "findings_rejected": 0,
        "repeated_source_reads": 0,
        "triage_calls": 1,
        "primary_review_calls": 3,
        "deep_review_calls": 0,
        "synthesis_calls": 1,
        "orchestration_steps_completed": 5,
        "orchestration_retries": 0,
    }
    assert valid_qa_task_metrics(event)
```

  Add parametrized rejections for `schema_version=True`, version 3, negative/bool stage counters, legacy model-family counters on v2, and a v1 event containing `triage_calls`. Add v2 orchestration cases showing `deep_review_calls > 0` only when the deep branch is selected, and partial/blocked outcomes with zero synthesis calls remain valid. Add a non-orchestrated v2 case with a nonzero `primary_review_calls` and assert rejection.

- [x] **Step 2: Run the focused event test and verify RED.**

  Run: `PYTHONPATH=src .venv/bin/pytest -q tests/test_events.py::test_v2_completed_orchestration_accepts_stage_counters`

  Expected: the v2 event is rejected by the current version-1-only counter contract.

- [x] **Step 3: Implement version-aware fields and validation.**

  In `events.py`, define `STAGE_CALL_COUNTERS` and `STAGE_TOKEN_COUNTERS` with the exact v2 names from Global Constraints. Keep the current model counters as the v1 set; define `LEGACY_MODEL_COUNTERS` as those model-token names plus `luna_calls`, `terra_calls`, and `sol_calls`; add both versions' fields to `EVENT_FIELDS` and the `JsonEventSink` payload allowlist; require an actual integer version in `{1, 2}` when a version is present; reject model-family fields from the wrong version; validate nonnegative integer counters without accepting booleans; and branch completed-flow/deep invariants by version. Keep absent version defaulting to v1 for internal legacy validation.

```python
schema_version = event.get("schema_version", 1)
if type(schema_version) is not int or schema_version not in {1, 2}:
    return False
if schema_version == 1 and (STAGE_CALL_COUNTERS | STAGE_TOKEN_COUNTERS) & event.keys():
    return False
if schema_version == 2 and LEGACY_MODEL_COUNTERS & event.keys():
    return False
```

  Define `LEGACY_MODEL_COUNTERS` as the union of v1 model-token fields and `{"luna_calls", "terra_calls", "sol_calls"}`; do not include shared `orchestration_steps_completed` or `orchestration_retries`. In `JsonEventSink.record_qa_task_outcome`, serialize the version supplied by the validated service event instead of hard-coding 1. Require the service-generated version rather than writing a new v1 row.

- [x] **Step 4: Switch the service and MCP input to the v2 stage interface.**

  In `service.py`, add keyword arguments `triage_calls`, `primary_review_calls`, `deep_review_calls`, `synthesis_calls`, and the six stage-token fields. Put `"schema_version": 2` into the internal event before validation. Keep shared `orchestration_steps_completed` and `orchestration_retries`; remove current model-family call/token inputs from the new service contract. Update `_orchestration_metric_errors` to use `deep_review_calls`, require the GPT-6 deep model, and compare completed flows against `len(session.review_profiles)`. Change current service/server test inputs to `gpt-6-sol`; keep `gpt-5.6-sol` only in explicit v1 event fixtures.

  Add matching `NonNegativeInt` arguments to `server.py`, use `SolModel = Literal["gpt-6-sol"]`, pass each argument by name to the service, and update the tool docstring to describe triage, N primary profiles, synthesis, and the optional additional deep step. Do not add a seventh tool.

- [x] **Step 5: Update service and MCP regression tests.**

  Update existing orchestration-metric test calls in `tests/test_service.py` and `tests/test_server.py` to the new stage counter names and use `gpt-6-sol` for new service/server deep outcomes. Add service assertions that persisted events use `schema_version == 2`, include stage fields, reject `gpt-5.6-sol` for a new v2 deep outcome, reject missing or extra deep-review calls, reject incorrect deep reasoning, and reject stage counts inconsistent with the live session. Keep explicit tests that schema v1 is still accepted by `valid_qa_task_metrics` and that the server publishes exactly six tools.

- [x] **Step 6: Run event/service/server tests and Ruff.**

  The report remains v1-only until Task 3, so run these task-local gates now:

  Run: `PYTHONPATH=src .venv/bin/pytest -q tests/test_events.py tests/test_service.py`

  Run: `PYTHONPATH=src .venv/bin/pytest -q tests/test_server.py -k 'not server_records_orchestration_metrics and not task_outcome_and_report_are_model_free'`

  Run: `PYTHONPATH=src .venv/bin/ruff check src/qa_orchestrator/events.py src/qa_orchestrator/service.py src/qa_orchestrator/server.py tests/test_events.py tests/test_service.py tests/test_server.py`

  Expected: v2 service writes validate and persist; v1 fixtures still validate; completed, partial, blocked, and deep flows preserve their current lifecycle rules. Run the two report-dependent MCP scenarios and the full server suite after Task 3.

### Task 3: Read and report mixed v1/v2 metric history

**Files:**

- Modify: `src/qa_orchestrator/report.py`
- Test: `tests/test_report.py`

**Interfaces:**

- Preserve the current `qa_tasks.model_tokens`, `qa_tasks.orchestration`, `deep_by_model`, and data-quality keys. Keep v1 model-token totals and legacy orchestration-call totals v1-only; keep top-level deep token totals across valid versions.
- Add `qa_tasks.stage_calls` with `triage`, `primary_review`, `deep_review`, and `synthesis` integer totals.
- Add `qa_tasks.stage_tokens` shaped as `{triage, primary_review, deep_review, synthesis}`, each with `input` and `output` totals; v2 `deep_review` reads from retained `deep_input_tokens` and `deep_output_tokens`.
- Model the new report fields with strict types: `StageCallsReport` has four integer fields; `StageTokensReport` has four `ModelTokenTotals` fields.
- Keep `qa_tasks.model_tokens` model-family totals v1-only. Keep shared task totals and the overall token-measurement rate across both valid schemas; v2 token completeness is based on all six v2 stage-token fields.

- [x] **Step 1: Add a mixed-history report test.**

  Extend `tests/test_report.py` with one valid v1 event using `gpt-5.6-sol` and one valid v2 event using `gpt-6-sol`; set v2 `deep_input_tokens=20` and `deep_output_tokens=30`. Assert both count toward `events` and `orchestration.tasks`, the old `luna_calls`/`terra_calls`/`sol_calls` totals include only v1 values, and the new stage summaries include only v2 values. Add a v2 row missing `synthesis_output_tokens` and assert it is not counted as a complete model-token measurement.

```python
assert report["qa_tasks"]["deep_by_model"] == {
    "gpt-5.6-sol": 1,
    "gpt-6-sol": 1,
}
assert report["qa_tasks"]["stage_calls"] == {
    "triage": 1,
    "primary_review": 3,
    "deep_review": 1,
    "synthesis": 1,
}
assert report["qa_tasks"]["stage_tokens"]["deep_review"] == {
    "input": 20,
    "output": 30,
}
```

- [x] **Step 2: Run the new report test and verify RED.**

  Run: `PYTHONPATH=src .venv/bin/pytest -q tests/test_report.py::test_report_keeps_v1_and_v2_metrics_separate`

  Expected: the existing report ignores schema version 2 and has no stage summaries.

- [x] **Step 3: Implement schema-aware report aggregation and response models.**

  In `summarize_events`, accept only schema versions 1 and 2 after `valid_qa_task_metrics`. For v1, aggregate the existing model-family call/token fields. For v2, aggregate stage calls/tokens. Aggregate `events`, outcomes, task type, orchestration task count, deep model/reasoning, deep duration/tokens, findings, and shared step/retry totals across both. Count model-token measurement complete only when the corresponding schema's full token field set is present. Keep `model_tokens.sol` v1-only; put v2 deep tokens only in `stage_tokens.deep_review`. Add strict Pydantic report models for the two new stage summaries and include them in `QaTasksReport`.

```python
schema_version = event.get("schema_version")
if type(schema_version) is not int or schema_version not in {1, 2}:
    continue
if schema_version == 1:
    for field in MODEL_TOKEN_COUNTERS | ORCHESTRATION_COUNTERS:
        totals[field] += event.get(field, 0)
else:
    for field in STAGE_CALL_COUNTERS | STAGE_TOKEN_COUNTERS | {
        "orchestration_steps_completed",
        "orchestration_retries",
    }:
        totals[field] += event.get(field, 0)
```

  Keep `deep_by_model` keyed by the exact string in each event. Do not add v2 stage calls to legacy `terra_calls` or `sol_calls`, and do not alter any stored row.

- [x] **Step 4: Run report tests and Ruff.**

  Run: `PYTHONPATH=src .venv/bin/pytest -q tests/test_report.py`

  Run: `PYTHONPATH=src .venv/bin/ruff check src/qa_orchestrator/report.py tests/test_report.py`

  Expected: v1 report snapshots remain unchanged; v2 totals appear only in stage summaries; the mixed-schema report passes `MetricsReport` validation.

### Task 4: Update current host instructions and lock the documented contract

**Files:**

- Modify: `README.md`
- Modify: `docs/ORCHESTRATION_POLICY.md`
- Modify: `docs/clients/codex.md`
- Modify: `docs/clients/claude-code.md`
- Modify: `docs/clients/cursor.md`
- Modify: `docs/clients/generic-mcp.md`
- Modify: `client-rules/generic/QA_ORCHESTRATOR_INSTRUCTIONS.md`
- Modify: `client-rules/claude-code/CLAUDE.md`
- Modify: `client-rules/cursor/qa-orchestrator.mdc`
- Test: `tests/test_install_artifacts.py`

**Interfaces:**

- Current policy text describes GPT-6 Luna/max triage, GPT-6 Sol/medium primary and synthesis, GPT-6 Sol/high optional deep review, and `speed=1.0`.
- Metric instructions use schema-v2 stage counter/token names and preserve the v1-read compatibility explanation.
- Preserve the six MCP tool names, host-owned decision boundary, Evidence Packet rules, and the exact approved escalation conditions.

- [x] **Step 1: Add documentation contract assertions before editing the current copies.**

  Update `CLIENT_RULE_CONTRACT` in `tests/test_install_artifacts.py` from GPT-5.6/Luna-Terra-Sol patterns to `gpt-6-luna`/Luna-max, `gpt-6-sol`/Sol-medium, and `gpt-6-sol`/Sol-high. Extend the artifact list in `test_operational_artifacts_describe_host_orchestration` to include `docs/clients/codex.md`, `docs/clients/claude-code.md`, `docs/clients/cursor.md`, and `docs/clients/generic-mcp.md`; require GPT-6 IDs and `speed=1.0` in those documents too. Update `test_operational_artifacts_describe_review_bundles_and_statuses` from `terra / medium` to `sol / medium`. Extend `test_client_rule_templates_preserve_orchestration_contract` to require every v2 stage-call/token field and to reject old model-family counter names in each current client template.

```python
for required in (
    "gpt-6-luna",
    "gpt-6-sol",
    "triage_calls",
    "primary_review_calls",
    "deep_review_calls",
    "synthesis_calls",
    "triage_input_tokens",
    "triage_output_tokens",
    "primary_review_input_tokens",
    "primary_review_output_tokens",
    "synthesis_input_tokens",
    "synthesis_output_tokens",
):
    assert required in text
assert "terra_calls" not in text
```

- [x] **Step 2: Run the documentation tests and verify RED.**

  Run: `PYTHONPATH=src .venv/bin/pytest -q tests/test_install_artifacts.py::test_operational_artifacts_describe_host_orchestration tests/test_install_artifacts.py::test_client_rule_templates_preserve_orchestration_contract`

  Expected: current templates fail the GPT-6 and v2 metric assertions.

- [x] **Step 3: Update only current policy/document copies.**

  Apply the approved policy table and stage counters to the listed files. Where a client document names `terra_primary_review` or `terra_synthesis`, explain that these are unchanged transition identifiers; the returned `model_policy` now assigns GPT-6 Sol. In README, edit only the model-policy/metrics descriptions; retain the existing “QA Orchestrator at a glance” section, both v2 PNG links, and all surrounding image content. Do not edit `docs/superpowers/specs/2026-09-19-qa-orchestrator-design.md` or `docs/superpowers/plans/2026-09-19-qa-orchestrator-plan.md`; they document the earlier GPT-5.6 design.

- [x] **Step 4: Run artifact tests and search for stale active instructions.**

  Run: `PYTHONPATH=src .venv/bin/pytest -q tests/test_install_artifacts.py`

  Run: `rg -n 'gpt-5\.6|luna_calls|terra_calls|sol_calls|terra_primary_(input|output)_tokens|terra_synthesis_(input|output)_tokens' README.md docs/ORCHESTRATION_POLICY.md docs/clients client-rules`

  Expected: current policy and client instruction files contain no active GPT-5.6 policy or v1 model-family counter instructions. Historical v1 examples may remain only where they explicitly explain backward-compatible stored history; the dated 2026-09-19 spec and plan are outside the search scope and remain untouched.

### Task 5: Run complete regression and inspect the final diff

**Files:**

- Verify: all files listed in Tasks 1–4
- Preserve: existing README images and unrelated working-tree changes

**Interfaces:**

- All six MCP tools remain published with the same names.
- New sessions return the approved GPT-6 model/reasoning mapping; new metric writes use schema v2; reports read both versions without attribution loss.

- [x] **Step 1: Run the full test suite and Ruff.**

  Run: `PYTHONPATH=src .venv/bin/pytest -q`

  Run: `PYTHONPATH=src .venv/bin/ruff check .`

  Expected: all tests pass and Ruff reports no errors.

- [x] **Step 2: Check whitespace, tool count, and the scoped diff.**

  Run: `git diff --check`

  Run: `PYTHONPATH=src .venv/bin/pytest -q tests/test_server.py::test_server_exposes_six_tools tests/test_install_artifacts.py::test_launcher_exposes_six_tools`

  Review `git status --short` and the complete diff from the repository root. Confirm the only new files are this implementation plan and the approved design spec plus the already-requested README PNG assets; confirm README image links are unchanged; confirm the dated 2026-09-19 files are untouched. Do not commit or push.

  Expected: no whitespace errors, both six-tool checks pass, and unrelated working-tree content remains intact.
