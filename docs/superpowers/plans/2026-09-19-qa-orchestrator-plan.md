# QA Router Host-Owned Orchestrator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deterministic host-owned QA orchestration state machine that coordinates Luna/max triage, Terra/medium review and optional Sol/high deep analysis without invoking models or external systems from QA Router.

**Architecture:** Add an in-memory `QaOrchestrator` with strict Pydantic contracts, fixed model policy metadata and explicit state transitions. Expose start/advance/get orchestration tools through the existing FastMCP server; the host client performs all model calls and submits only structured, content-free transition signals. Extend the existing metrics sink with aggregate orchestration counters and update client documentation.

**Tech Stack:** Python 3.12, FastMCP, Pydantic, pytest/pytest-asyncio, Ruff, in-memory session state, JSONL metrics.

**Spec:** `docs/superpowers/specs/2026-09-19-qa-orchestrator-design.md`

## Global Constraints

- `gpt-5.6-luna` with `max` reasoning is used only for host-owned triage.
- `gpt-5.6-terra` with `medium` reasoning is used for primary review and synthesis.
- `gpt-5.6-sol` with `high` reasoning is an optional host-owned read-only escalation.
- QA Router must not invoke, configure, load, tokenize, or communicate with any model.
- The router must not retrieve source-system data, accept Evidence Packets, store task content, or perform external writes.
- Sessions are in-memory, content-free, bounded by TTL and maximum count, and are discarded on server restart.
- The host calls `record_qa_task_outcome` once after `awaiting_host_outcome`, `partial`, or `blocked`.
- Invalid profiles, signals, transitions, expired sessions, and unknown run IDs fail closed without mutating state.
- No hidden fallback tier or automatic model retry is added.

## Review Focus

- A repeated, skipped, or out-of-order step must not advance the session or alter its state — cover in Task 2 transition tests.
- An expired or unknown `run_id` must return a typed error without leaking session data — cover in Task 2 expiry tests.
- Free-form evidence, prompts, model outputs, and arbitrary reason text must be rejected by orchestration inputs — cover in Task 1 contract tests and Task 3 MCP tests.
- A deep-analysis request without a fixed reason code, or a reason code when the Sol branch is not requested, must fail closed — cover in Task 2 signal-validation tests.
- Negative, inconsistent, or content-bearing orchestration metrics must be rejected or omitted — cover in Task 4 event/report tests.

---

### Task 1: Orchestration Contracts and Model Policy

**Files:**
- Create: `src/qa_router_mcp/orchestration.py`
- Modify: `src/qa_router_mcp/contracts.py`
- Test: `tests/test_orchestration_contracts.py`

**Interfaces:**
- Consumes: existing `QaTaskType`, `ReviewAgent`, and `QaTaskOutcome` aliases.
- Produces: `OrchestrationStep`, `OrchestrationStatus`, `OrchestrationModel`, `OrchestrationReason`, `ModelPolicy`, `QaOrchestrationSession`, `AdvanceQaOrchestrationRequest`, and immutable `MODEL_POLICIES` for Tasks 2–5.

- [ ] **Step 1: Write failing contract tests for the fixed model policy**

```python
from pydantic import ValidationError
import pytest

from qa_router_mcp.contracts import ReviewAgent
from qa_router_mcp.orchestration import (
    AdvanceQaOrchestrationRequest,
    MODEL_POLICIES,
    OrchestrationModel,
    OrchestrationReason,
    OrchestrationStep,
)


def test_model_policy_assigns_requested_models_and_reasoning():
    assert MODEL_POLICIES[OrchestrationStep.LUNA_TRIAGE].model == OrchestrationModel.LUNA
    assert MODEL_POLICIES[OrchestrationStep.LUNA_TRIAGE].reasoning == "max"
    assert MODEL_POLICIES[OrchestrationStep.TERRA_PRIMARY_REVIEW].model == OrchestrationModel.TERRA
    assert MODEL_POLICIES[OrchestrationStep.TERRA_PRIMARY_REVIEW].reasoning == "medium"
    assert MODEL_POLICIES[OrchestrationStep.SOL_DEEP_REVIEW].model == OrchestrationModel.SOL
    assert MODEL_POLICIES[OrchestrationStep.SOL_DEEP_REVIEW].reasoning == "high"


def test_advance_request_rejects_free_form_fields():
    with pytest.raises(ValidationError):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.LUNA_TRIAGE,
            status="completed",
            selected_profile=ReviewAgent.CODE_REVIEWER,
            evidence="raw source text",
        )


def test_deep_reason_requires_fixed_reason_code():
    with pytest.raises(ValidationError):
        AdvanceQaOrchestrationRequest(
            run_id="qar-0123456789abcdef0123456789abcdef",
            completed_step=OrchestrationStep.TERRA_PRIMARY_REVIEW,
            status="completed",
            needs_deep_analysis=True,
        )

    request = AdvanceQaOrchestrationRequest(
        run_id="qar-0123456789abcdef0123456789abcdef",
        completed_step=OrchestrationStep.TERRA_PRIMARY_REVIEW,
        status="completed",
        needs_deep_analysis=True,
        reason_code=OrchestrationReason.SECURITY_SENSITIVE,
    )
    assert request.reason_code is OrchestrationReason.SECURITY_SENSITIVE
```

- [ ] **Step 2: Run the contract tests and verify RED**

Run: `.venv/bin/pytest -q tests/test_orchestration_contracts.py`

Expected: FAIL because the orchestration contracts and model policy do not exist.

- [ ] **Step 3: Implement the strict contracts and immutable policy**

Add to `src/qa_router_mcp/orchestration.py`:

```python
class OrchestrationStep(StrEnum):
    LUNA_TRIAGE = "luna_triage"
    TERRA_PRIMARY_REVIEW = "terra_primary_review"
    SOL_DEEP_REVIEW = "sol_deep_review"
    TERRA_SYNTHESIS = "terra_synthesis"
    AWAITING_HOST_OUTCOME = "awaiting_host_outcome"


class OrchestrationStatus(StrEnum):
    ACTIVE = "active"
    AWAITING_HOST_OUTCOME = "awaiting_host_outcome"
    COMPLETED = "completed"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    EXPIRED = "expired"


class OrchestrationModel(StrEnum):
    LUNA = "gpt-5.6-luna"
    TERRA = "gpt-5.6-terra"
    SOL = "gpt-5.6-sol"


class OrchestrationReason(StrEnum):
    EVIDENCE_GAP = "evidence_gap"
    CROSS_REPOSITORY = "cross_repository"
    SECURITY_SENSITIVE = "security_sensitive"
    PAYMENT_SENSITIVE = "payment_sensitive"
    ROOT_CAUSE = "root_cause"
    HIGH_BLAST_RADIUS = "high_blast_radius"
```

Use `ConfigDict(extra="forbid")` on input/session models, `Field` constraints for `run_id`, and `MappingProxyType` for `MODEL_POLICIES`. The model policy must contain exactly Luna/max, Terra/medium and Sol/high. Keep all user-visible next-action strings constant; do not accept arbitrary text.

Define the models with these exact fields:

```python
class ModelPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model: OrchestrationModel
    reasoning: Literal["medium", "high", "max"]


class QaOrchestrationSession(BaseModel):
    run_id: str
    task_type: QaTaskType
    status: OrchestrationStatus
    current_step: OrchestrationStep
    selected_profile: ReviewAgent | None = None
    model_policy: ModelPolicy | None = None
    next_action: str
    read_only: bool = True
    host_owns_decisions: bool = True
    expires_at: datetime


class AdvanceQaOrchestrationRequest(BaseModel):
    run_id: str
    completed_step: OrchestrationStep
    status: QaTaskOutcome
    selected_profile: ReviewAgent | None = None
    needs_deep_analysis: bool = False
    reason_code: OrchestrationReason | None = None
```

Use an after-validator to require `selected_profile` after a completed Luna step, require both `needs_deep_analysis=true` and `reason_code` for a Sol request, and reject a reason code when the Sol branch is not requested.

- [ ] **Step 4: Run the contract tests and verify GREEN**

Run: `.venv/bin/pytest -q tests/test_orchestration_contracts.py && .venv/bin/ruff check src/qa_router_mcp/orchestration.py src/qa_router_mcp/contracts.py`

Expected: all contract tests pass and Ruff reports no errors.

- [ ] **Step 5: Commit the contracts**

```bash
git add src/qa_router_mcp/orchestration.py src/qa_router_mcp/contracts.py tests/test_orchestration_contracts.py
git commit -m "feat: define qa orchestration contracts"
```

### Task 2: In-Memory Orchestration State Machine

**Files:**
- Modify: `src/qa_router_mcp/orchestration.py`
- Test: `tests/test_orchestration.py`

**Interfaces:**
- Consumes: Task 1 enums, `ModelPolicy`, `QaOrchestrationSession`, and `AdvanceQaOrchestrationRequest`.
- Produces: `QaOrchestrator.start(task_type)`, `QaOrchestrator.advance(*, run_id, completed_step, status, selected_profile=None, needs_deep_analysis=False, reason_code=None)`, `QaOrchestrator.get(run_id)`, and `OrchestrationError` for Tasks 3–5.

- [ ] **Step 1: Write failing state-machine tests for the normal path**

```python
from qa_router_mcp.orchestration import (
    OrchestrationStatus,
    OrchestrationStep,
    QaOrchestrator,
)


def test_normal_flow_skips_sol():
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review")

    assert session.current_step is OrchestrationStep.LUNA_TRIAGE
    assert session.model_policy.model.value == "gpt-5.6-luna"
    assert session.model_policy.reasoning == "max"

    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.LUNA_TRIAGE,
        status="completed",
        selected_profile="code_reviewer",
    )
    assert session.current_step is OrchestrationStep.TERRA_PRIMARY_REVIEW
    assert session.model_policy.reasoning == "medium"

    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.TERRA_PRIMARY_REVIEW,
        status="completed",
        needs_deep_analysis=False,
    )
    assert session.current_step is OrchestrationStep.TERRA_SYNTHESIS

    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.TERRA_SYNTHESIS,
        status="completed",
    )
    assert session.status is OrchestrationStatus.AWAITING_HOST_OUTCOME
    assert session.current_step is OrchestrationStep.AWAITING_HOST_OUTCOME
    assert session.model_policy is None
```

- [ ] **Step 2: Add failing tests for Sol escalation and terminal failures**

```python
import pytest

from qa_router_mcp.orchestration import (
    OrchestrationError,
    OrchestrationStatus,
    OrchestrationStep,
    QaOrchestrator,
)


def test_deep_flow_uses_sol_then_returns_to_terra():
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review")
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.LUNA_TRIAGE,
        status="completed",
        selected_profile="security_reviewer",
    )
    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.TERRA_PRIMARY_REVIEW,
        status="completed",
        needs_deep_analysis=True,
        reason_code="security_sensitive",
    )

    assert session.current_step is OrchestrationStep.SOL_DEEP_REVIEW
    assert session.model_policy.model.value == "gpt-5.6-sol"
    assert session.model_policy.reasoning == "high"

    session = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.SOL_DEEP_REVIEW,
        status="completed",
    )
    assert session.current_step is OrchestrationStep.TERRA_SYNTHESIS


Add a parametrized test over every `ReviewAgent` value to verify that all seven fixed profiles are accepted after Luna triage, retained in the session, and continue to use the Terra/medium primary-review policy.


def test_illegal_transition_does_not_mutate_session():
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review")

    with pytest.raises(OrchestrationError, match="illegal transition"):
        orchestrator.advance(
            run_id=session.run_id,
            completed_step=OrchestrationStep.TERRA_PRIMARY_REVIEW,
            status="completed",
        )

    current = orchestrator.get(session.run_id)
    assert current.current_step is OrchestrationStep.LUNA_TRIAGE
    assert current.status is OrchestrationStatus.ACTIVE


def test_partial_or_blocked_step_is_terminal():
    orchestrator = QaOrchestrator(ttl_seconds=1800, max_sessions=10)
    session = orchestrator.start("ordinary_review")

    blocked = orchestrator.advance(
        run_id=session.run_id,
        completed_step=OrchestrationStep.LUNA_TRIAGE,
        status="blocked",
    )

    assert blocked.status is OrchestrationStatus.BLOCKED
    with pytest.raises(OrchestrationError, match="terminal"):
        orchestrator.advance(
            run_id=session.run_id,
            completed_step=OrchestrationStep.LUNA_TRIAGE,
            status="completed",
            selected_profile="code_reviewer",
        )
```

- [ ] **Step 3: Run the state-machine tests and verify RED**

Run: `.venv/bin/pytest -q tests/test_orchestration.py`

Expected: FAIL because `QaOrchestrator` and transition validation are not implemented.

- [ ] **Step 4: Implement bounded in-memory state and transitions**

Implement `QaOrchestrator` with a private dictionary of sessions, injected clock, TTL cleanup before `start`, `get`, and `advance`, and a maximum-session guard. Generate IDs as `qar-` plus 32 lowercase hexadecimal characters. Implement only these transitions:

```text
LUNA_TRIAGE + completed + selected_profile -> TERRA_PRIMARY_REVIEW
TERRA_PRIMARY_REVIEW + completed + needs_deep_analysis=false -> TERRA_SYNTHESIS
TERRA_PRIMARY_REVIEW + completed + needs_deep_analysis=true + reason_code -> SOL_DEEP_REVIEW
SOL_DEEP_REVIEW + completed -> TERRA_SYNTHESIS
TERRA_SYNTHESIS + completed -> AWAITING_HOST_OUTCOME
any active step + partial|blocked -> terminal matching status
```

Reject a reason code without `needs_deep_analysis`, a deep request without a reason code, a changed profile after triage, missing profile after Luna, repeated steps, terminal-session advances, unknown IDs, expired IDs, and incompatible task/profile values. Return a copy of the Pydantic session so callers cannot mutate the store through a returned object.

- [ ] **Step 5: Add expiry and bounded-session tests, then make them pass**

Use a mutable `now` closure passed to `QaOrchestrator(clock=...)` and assert that advancing time beyond `ttl_seconds` makes `get(run_id)` raise `OrchestrationError("expired session")`. Start `max_sessions` sessions, then assert the next `start` raises `OrchestrationError("session limit")`; after advancing the clock beyond TTL, assert a new session can be created.

Run: `.venv/bin/pytest -q tests/test_orchestration_contracts.py tests/test_orchestration.py`

Expected: all contract, normal-path, deep-path, failure, expiry and limit tests pass.

- [ ] **Step 6: Commit the state machine**

```bash
git add src/qa_router_mcp/orchestration.py tests/test_orchestration.py
git commit -m "feat: add qa orchestration state machine"
```

### Task 3: Service and MCP Orchestration Surface

**Files:**
- Modify: `src/qa_router_mcp/config.py`
- Modify: `src/qa_router_mcp/service.py`
- Modify: `src/qa_router_mcp/server.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_service.py`
- Modify: `tests/test_server.py`
- Modify: `tests/test_install_artifacts.py`

**Interfaces:**
- Consumes: `QaOrchestrator.start`, `.advance`, `.get`, contracts from Tasks 1–2, and existing `RouterService`/FastMCP construction.
- Produces: MCP tools `start_qa_orchestration`, `advance_qa_orchestration`, and `get_qa_orchestration`; total tool set becomes the existing three tools plus these three.

- [ ] **Step 1: Write failing service and MCP-surface tests**

```python
@pytest.mark.asyncio
async def test_server_exposes_six_tools(tmp_path):
    service = RouterService.from_settings(data_dir=tmp_path)

    async with Client(build_server(service)) as client:
        names = {tool.name for tool in await client.list_tools()}

    assert names == {
        "prepare_review_route",
        "record_qa_task_outcome",
        "get_metrics_report",
        "start_qa_orchestration",
        "advance_qa_orchestration",
        "get_qa_orchestration",
    }


@pytest.mark.asyncio
async def test_orchestration_tools_return_no_evidence_fields(tmp_path):
    service = RouterService.from_settings(data_dir=tmp_path)

    async with Client(build_server(service)) as client:
        result = await client.call_tool(
            "start_qa_orchestration",
            {"task_type": "ordinary_review"},
        )

    payload = result.structured_content
    assert payload["model_policy"] == {
        "model": "gpt-5.6-luna",
        "reasoning": "max",
    }
    assert "evidence" not in payload
    assert "prompt" not in payload
    assert "output" not in payload
```

- [ ] **Step 2: Run the focused service/server tests and verify RED**

Run: `.venv/bin/pytest -q tests/test_service.py tests/test_server.py tests/test_install_artifacts.py`

Expected: FAIL because the three orchestration tools and settings do not exist, while the current tests still expect only the original three tools.

- [ ] **Step 3: Add session settings and wire the orchestrator into `RouterService`**

Add to `Settings`:

```python
orchestration_session_ttl_seconds: int = 1_800
orchestration_max_sessions: int = 100
```

Read `QA_ROUTER_ORCHESTRATION_TTL_SECONDS` and `QA_ROUTER_ORCHESTRATION_MAX_SESSIONS` in `Settings.from_env`; reject non-positive values. Construct one `QaOrchestrator` in `RouterService.__init__` and add thin wrappers with these signatures:

```python
def start_qa_orchestration(self, task_type: QaTaskType) -> QaOrchestrationSession: ...

def advance_qa_orchestration(
    self,
    request: AdvanceQaOrchestrationRequest,
) -> QaOrchestrationSession: ...

def get_qa_orchestration(self, run_id: str) -> QaOrchestrationSession: ...
```

The FastMCP wrapper constructs `AdvanceQaOrchestrationRequest` from its explicit typed parameters, then calls the service wrapper; the service forwards the validated fields to `QaOrchestrator.advance(**request.model_dump())`. No free-form field is accepted at either boundary.

- [ ] **Step 4: Register the three MCP tools without passing free-form content**

Add synchronous FastMCP wrappers with explicit typed parameters. `advance_qa_orchestration` accepts `run_id: str`, `completed_step: OrchestrationStep`, `status: QaTaskOutcome`, `selected_profile: ReviewAgent | None`, `needs_deep_analysis: bool`, and `reason_code: OrchestrationReason | None`; `start_qa_orchestration` accepts only `task_type: QaTaskType`, and `get_qa_orchestration` accepts only `run_id: str`. The server must pass structured values directly to `RouterService`; it must not accept an Evidence Packet, arbitrary prompt, model output, or free-form reason. Keep `prepare_review_route`, `record_qa_task_outcome`, and `get_metrics_report` unchanged except for the metrics additions in Task 4.

- [ ] **Step 5: Run service, MCP, launcher and config tests and verify GREEN**

Run: `.venv/bin/pytest -q tests/test_config.py tests/test_service.py tests/test_server.py tests/test_install_artifacts.py && .venv/bin/ruff check src tests`

Expected: all tests pass, the launcher remains valid, and the MCP tool list contains exactly six tools.

- [ ] **Step 6: Commit the MCP surface**

```bash
git add src/qa_router_mcp/config.py src/qa_router_mcp/service.py src/qa_router_mcp/server.py tests/test_config.py tests/test_service.py tests/test_server.py tests/test_install_artifacts.py
git commit -m "feat: expose qa orchestration tools"
```

### Task 4: Content-Free Orchestration Metrics

**Files:**
- Modify: `src/qa_router_mcp/events.py`
- Modify: `src/qa_router_mcp/service.py`
- Modify: `src/qa_router_mcp/server.py`
- Modify: `src/qa_router_mcp/report.py`
- Modify: `tests/test_report.py`
- Modify: `tests/test_service.py`
- Modify: `tests/test_server.py`

**Interfaces:**
- Consumes: existing task metrics validation/reporting and the completed orchestration session contract.
- Produces: optional content-free fields `orchestration_used`, `luna_calls`, `terra_calls`, `sol_calls`, `orchestration_steps_completed`, and `orchestration_retries` in the outcome tool and report.

- [ ] **Step 1: Write failing metrics tests**

```python
def test_report_aggregates_orchestration_counters():
    line = json.dumps(
        {
            "schema_version": 1,
            "event_type": "qa_task_outcome",
            "timestamp": datetime.now(UTC).isoformat(),
            "task_type": "ordinary_review",
            "outcome": "completed",
            "deep_analysis_used": True,
            "deep_model": "gpt-5.6-sol",
            "deep_reasoning": "high",
            "codegraph_calls": 1,
            "source_mcp_calls": 2,
            "findings_identified": 1,
            "findings_confirmed": 1,
            "findings_rejected": 0,
            "repeated_source_reads": 0,
            "orchestration_used": True,
            "luna_calls": 1,
            "terra_calls": 2,
            "sol_calls": 1,
            "orchestration_steps_completed": 4,
            "orchestration_retries": 0,
        }
    )

    report = summarize_events([line])

    assert report["qa_tasks"]["orchestration"] == {
        "tasks": 1,
        "luna_calls": 1,
        "terra_calls": 2,
        "sol_calls": 1,
        "steps_completed": 4,
        "retries": 0,
    }


def test_metrics_reject_negative_orchestration_counter(tmp_path):
    service = RouterService.from_settings(data_dir=tmp_path)

    with pytest.raises(ValueError, match="QA task metrics are inconsistent"):
        service.record_qa_task_outcome(
            task_type="ordinary_review",
            outcome="completed",
            codegraph_calls=0,
            source_mcp_calls=0,
            findings_identified=0,
            findings_confirmed=0,
            findings_rejected=0,
            repeated_source_reads=0,
            orchestration_used=True,
            luna_calls=-1,
        )
```

- [ ] **Step 2: Run the metrics tests and verify RED**

Run: `.venv/bin/pytest -q tests/test_report.py tests/test_service.py tests/test_server.py`

Expected: FAIL because the event whitelist, service signature and report do not contain orchestration counters.

- [ ] **Step 3: Extend event validation and persistence**

Add a dedicated `ORCHESTRATION_COUNTERS` set and an `orchestration_used` boolean to `events.py`. Include the fields in the event whitelist. Require exact non-negative integers for counters; reject counters when `orchestration_used` is false and any counter is positive. Preserve the existing no-unknown-fields, findings consistency, source/CodeGraph consistency, retention, locking and content-free guarantees.

- [ ] **Step 4: Wire metrics through service/server and aggregate the report**

Add the optional parameters to `RouterService.record_qa_task_outcome` and the FastMCP tool. Add an `orchestration` object under `qa_tasks` in `summarize_events` with task count and the five aggregate counters. Do not include prompts, outputs, run IDs, task identifiers, paths, or arbitrary model metadata.

- [ ] **Step 5: Run the metrics and full focused checks**

Run: `.venv/bin/pytest -q tests/test_report.py tests/test_service.py tests/test_server.py tests/test_install_artifacts.py && .venv/bin/ruff check .`

Expected: all tests pass and the report contains only aggregate orchestration data.

- [ ] **Step 6: Commit metrics support**

```bash
git add src/qa_router_mcp/events.py src/qa_router_mcp/service.py src/qa_router_mcp/server.py src/qa_router_mcp/report.py tests/test_report.py tests/test_service.py tests/test_server.py
git commit -m "feat: record orchestration metrics"
```

### Task 5: Documentation, Client Rules, and Complete Verification

**Files:**
- Modify: `README.md`
- Modify: `CONTRIBUTING.md`
- Modify: `docs/ROUTING_POLICY.md`
- Modify: `docs/clients/codex.md`
- Modify: `docs/clients/claude-code.md`
- Modify: `docs/clients/cursor.md`
- Modify: `docs/clients/generic-mcp.md`
- Modify: `client-rules/claude-code/CLAUDE.md`
- Modify: `client-rules/cursor/qa-router.mdc`
- Modify: `client-rules/generic/QA_ROUTER_INSTRUCTIONS.md`
- Modify: `tests/test_install_artifacts.py`
- Test: complete repository suite

**Interfaces:**
- Consumes: the six-tool MCP surface, model policy, state transitions, host-owned boundaries and metrics fields from Tasks 1–4.
- Produces: operational docs that describe Luna/max → Terra/medium → optional Sol/high → Terra/medium, without instructing clients to call models from QA Router or perform autonomous writes.

- [ ] **Step 1: Write failing documentation assertions**

Extend `tests/test_install_artifacts.py` so README, routing policy and generic client instructions must contain `start_qa_orchestration`, `advance_qa_orchestration`, `get_qa_orchestration`, `gpt-5.6-luna`, `gpt-5.6-terra`, `gpt-5.6-sol`, and `host_owns_decisions`, while not describing model calls from the router or arbitrary evidence submission.

- [ ] **Step 2: Run the documentation test and verify RED**

Run: `.venv/bin/pytest -q tests/test_install_artifacts.py::test_operational_artifacts_describe_host_orchestration`

Expected: FAIL because the current docs describe the three-tool route-only service and do not document the orchestration tools or model policy.

- [ ] **Step 3: Update operational docs and client rules**

Document the exact six-tool surface, the host-owned model assignments, state transitions, optional Sol branch, in-memory session/TTL behavior, content-free metrics, and the rule that primary host owns evidence, decisions and writes. Keep the existing no-local-runtime boundary intact and do not add model credentials or source-system instructions to QA Router.

- [ ] **Step 4: Run the documentation test and static checks**

Run: `.venv/bin/pytest -q tests/test_install_artifacts.py::test_operational_artifacts_describe_host_orchestration && .venv/bin/ruff check .`

Expected: PASS and Ruff reports no errors.

- [ ] **Step 5: Run complete verification**

Run:

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
git diff --check
git status --short --branch
```

Also run a repository-scoped scan for retired generation APIs and confirm the CodeGraph index has no pending changes:

```bash
/Applications/ChatGPT.app/Contents/Resources/rg -n -i "draft_review_checklist|record_canary_feedback|shadow_evaluation_required|generation_stats" \
  --glob '!docs/superpowers/plans/2026-09-19-qa-orchestrator-plan.md' .
/Users/andreiviarshko/.local/bin/codegraph status --json .
```

Expected: zero test failures, Ruff success, clean diff/status, no retired generation API references, and `pendingChanges` equal to zero.

- [ ] **Step 6: Commit documentation and final migration**

```bash
git add README.md CONTRIBUTING.md docs client-rules tests/test_install_artifacts.py
git commit -m "docs: describe host-owned qa orchestration"
```
