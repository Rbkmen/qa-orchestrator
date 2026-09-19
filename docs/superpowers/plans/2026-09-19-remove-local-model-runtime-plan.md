# Remove Local Model Runtime and Make QA Router Deterministic Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the Qwen/LM Studio generation runtime and expose deterministic review-profile routing plus content-free QA task metrics.

**Architecture:** `qa-router` becomes a small FastMCP service with a static `ReviewAgent` registry, a `prepare_review_route` contract, and a file-backed task-outcome metric sink. The primary Codex owns source retrieval, review execution, findings, optional `qa_deep`, and every external action; the router performs no model calls.

**Tech Stack:** Python 3.12, FastMCP, Pydantic, pytest/pytest-asyncio, Ruff, JSONL metrics.

**Spec:** `docs/superpowers/specs/2026-09-19-remove-local-model-runtime-design.md`

## Global Constraints

- QA Router must not import, configure, start, tokenize, or call Qwen, LM Studio, MLX, llmster, or any other model runtime.
- The only MCP tools after migration are `prepare_review_route`, `record_qa_task_outcome`, and `get_metrics_report`.
- Review routes are deterministic metadata; they do not create agents, threads, drafts, findings, or external writes.
- Metrics remain content-free and contain no evidence, prompts, drafts, issue keys, paths, or logs.
- The existing seven `ReviewAgent` values remain stable and must each return a valid route.
- The primary Codex remains the owner of evidence, confirmation, severity, readiness, runtime proof, and external-system writes.

## Review Focus

- Unknown or missing profile must fail before any route is returned — test invalid enum/tool input in Task 1.
- The MCP surface must not expose retired drafting or canary tools — test the exact tool set in Task 2.
- Task metrics must reject negative, inconsistent, or Qwen-specific fields — test valid and invalid events in Task 2.
- The launcher must start the server without a model, loopback endpoint, or LM environment — test shell syntax and stdio startup in Task 3.
- Documentation and client rules must not instruct a host to use local generation — test artifact fragments in Task 4.

---

### Task 1: Deterministic Review Route Contract

**Files:**
- Create: `src/qa_router_mcp/review_profiles.py`
- Modify: `src/qa_router_mcp/contracts.py`
- Modify: `src/qa_router_mcp/config.py`
- Modify: `src/qa_router_mcp/service.py`
- Test: `tests/test_review_profiles.py`
- Test: `tests/test_contracts.py`

**Interfaces:**
- Consumes: existing `ReviewAgent` enum and seven profile focus descriptions.
- Produces: `ReviewRoute` Pydantic model and `RouterService.prepare_review_route(agent_profile: ReviewAgent | str) -> ReviewRoute`.

- [ ] **Step 1: Write the failing route-contract tests**

```python
import pytest

from qa_router_mcp.contracts import ReviewAgent, ReviewRoute
from qa_router_mcp.service import RouterService


def test_every_review_profile_returns_read_only_route(tmp_path):
    service = RouterService.from_settings(data_dir=tmp_path)

    for profile in ReviewAgent:
        route = service.prepare_review_route(profile)

        assert isinstance(route, ReviewRoute)
        assert route.profile == profile
        assert route.read_only is True
        assert route.host_owns_decisions is True
        assert route.required_sections == [
            "Scope",
            "Checklist",
            "Candidate Coverage Gaps",
            "Positive Observations",
            "Unverified",
        ]
        assert route.focus


def test_unknown_review_profile_is_rejected(tmp_path):
    service = RouterService.from_settings(data_dir=tmp_path)

    with pytest.raises(ValueError, match="unknown review agent profile"):
        service.prepare_review_route("unknown_profile")
```

- [ ] **Step 2: Run the focused tests and verify the expected RED failure**

Run: `.venv/bin/pytest -q tests/test_review_profiles.py tests/test_contracts.py`

Expected: FAIL because `ReviewRoute` and the deterministic service method do not exist yet.

- [ ] **Step 3: Implement the minimal deterministic contract**

Define `ReviewRoute` with `profile`, `focus`, `required_sections`, `constraints`, `escalation_signals`, `read_only`, and `host_owns_decisions`. Move the seven static focus strings into `review_profiles.py`; use a single immutable map and a shared required-section tuple. Simplify `Settings` to metrics configuration and give `RouterService` a `from_settings` constructor plus the profile resolver. Do not retain generation, backend, token, policy, or quality-gate dependencies in the new route path.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run: `.venv/bin/pytest -q tests/test_review_profiles.py tests/test_contracts.py`

Expected: all focused route and contract tests pass.

- [ ] **Step 5: Commit the contract**

```bash
git add src/qa_router_mcp/review_profiles.py src/qa_router_mcp/contracts.py src/qa_router_mcp/config.py src/qa_router_mcp/service.py tests/test_review_profiles.py tests/test_contracts.py
git commit -m "feat: add deterministic review route contract"
```

---

### Task 2: Model-Free MCP Server and Metrics

**Files:**
- Modify: `src/qa_router_mcp/server.py`
- Modify: `src/qa_router_mcp/service.py`
- Modify: `src/qa_router_mcp/events.py`
- Modify: `src/qa_router_mcp/report.py`
- Modify: `tests/test_server.py`
- Modify: `tests/test_service.py`
- Modify: `tests/test_report.py`
- Modify: `tests/test_install_artifacts.py`

**Interfaces:**
- Consumes: `ReviewRoute`, `RouterService.prepare_review_route`, and the existing content-free QA task counters.
- Produces: MCP tools `prepare_review_route`, `record_qa_task_outcome`, and `get_metrics_report`; no drafting or canary tools.

- [ ] **Step 1: Write the failing MCP-surface and metrics tests**

```python
@pytest.mark.asyncio
async def test_server_exposes_only_deterministic_route_and_metrics_tools(tmp_path):
    service = RouterService.from_settings(data_dir=tmp_path)

    async with Client(build_server(service)) as client:
        names = {tool.name for tool in await client.list_tools()}

    assert names == {
        "prepare_review_route",
        "record_qa_task_outcome",
        "get_metrics_report",
    }
```

Also change the task-outcome test fixture to omit `qwen_used` and `qwen_edits`, and assert that the stored/report output has no model, generation, token, canary, or Qwen fields.

- [ ] **Step 2: Run the focused tests and verify the expected RED failure**

Run: `.venv/bin/pytest -q tests/test_server.py tests/test_service.py tests/test_report.py tests/test_install_artifacts.py`

Expected: FAIL because the current server still registers local drafting and canary tools and metrics still require Qwen-era fields.

- [ ] **Step 3: Implement the model-free service, event sink, report, and server**

Remove the draft-generation path from `RouterService`, including backend injection, generation envelopes, policy sanitization for local calls, validation repair, quality gates, canary feedback, and generation timing. Keep file locking, retention, and content-free QA task recording. Register only `prepare_review_route`, `record_qa_task_outcome`, and `get_metrics_report`. Simplify report aggregation to task outcomes, task types, CodeGraph/source counters, findings counters, repeated reads, and optional deep-analysis measurements.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run: `.venv/bin/pytest -q tests/test_server.py tests/test_service.py tests/test_report.py tests/test_install_artifacts.py`

Expected: all focused MCP, metric, and install-artifact tests pass.

- [ ] **Step 5: Commit the model-free MCP surface**

```bash
git add src/qa_router_mcp/server.py src/qa_router_mcp/service.py src/qa_router_mcp/events.py src/qa_router_mcp/report.py tests/test_server.py tests/test_service.py tests/test_report.py tests/test_install_artifacts.py
git commit -m "refactor: make qa router model free"
```

---

### Task 3: Remove Runtime, Dependencies, and Legacy Tests

**Files:**
- Delete: `src/qa_router_mcp/backends.py`
- Delete: `src/qa_router_mcp/prompts.py`
- Delete: `src/qa_router_mcp/validation.py`
- Delete: `src/qa_router_mcp/quality.py`
- Delete: `tests/test_backends.py`
- Delete: `tests/test_live_lmstudio.py`
- Delete: `tests/test_live_extended.py`
- Delete: `tests/test_quality.py`
- Delete: `tests/test_token_metrics.py`
- Modify: `tests/test_contracts.py`
- Delete: `launchd/com.qa-router.llmster.plist`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `scripts/qa-router-mcp`
- Modify: `tests/test_config.py`

**Interfaces:**
- Consumes: the model-free server from Task 2.
- Produces: a package that imports and starts without LM Studio, `lmstudio`, `httpx`, model environment variables, or launchd runtime artifacts.

- [ ] **Step 1: Add failing no-runtime checks**

```python
def test_settings_have_no_model_runtime_configuration():
    settings = Settings(data_dir=Path("/tmp/qa-router-test"))

    assert not hasattr(settings, "model")
    assert not hasattr(settings, "lmstudio_url")
    assert not hasattr(settings, "context")


def test_launcher_does_not_reference_model_runtime():
    content = (ROOT / "scripts/qa-router-mcp").read_text(encoding="utf-8")

    assert "QA_ROUTER_MODEL" not in content
    assert "LMSTUDIO" not in content
    assert "127.0.0.1:1234" not in content
```

- [ ] **Step 2: Run the focused tests and verify the expected RED failure**

Run: `.venv/bin/pytest -q tests/test_config.py tests/test_install_artifacts.py`

Expected: FAIL because settings and the launcher still expose the retired runtime.

- [ ] **Step 3: Delete the runtime and refresh dependencies**

Remove the four model/generation modules and their tests. Remove direct `httpx` and `lmstudio` dependencies from `pyproject.toml`, regenerate `uv.lock` with `uv lock`, and rewrite the launcher to pass only the metrics data directory/retention settings before executing the MCP entry point. Remove the llmster plist.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run: `.venv/bin/pytest -q tests/test_config.py tests/test_install_artifacts.py`

Expected: all no-runtime and launcher tests pass.

- [ ] **Step 5: Commit runtime removal**

```bash
git add -A src tests pyproject.toml uv.lock scripts launchd
git commit -m "chore: remove local model runtime"
```

---

### Task 4: Documentation and Client Routing Migration

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
- Delete: `codex/skills/qa-local-routing/SKILL.md`
- Delete: `docs/assets/qa-router-overview.svg`
- Delete: `docs/assets/qa-router-workflow.svg`
- Delete: `docs/superpowers/specs/2026-09-03-router-quality-measurement-design.md`
- Delete: `docs/superpowers/plans/2026-09-03-router-quality-measurement.md`
- Delete: `docs/superpowers/specs/2026-09-19-ecc-review-agent-profiles-design.md`
- Delete: `docs/superpowers/plans/2026-09-19-ecc-review-agent-profiles-plan.md`
- Modify: `tests/test_install_artifacts.py`

**Interfaces:**
- Consumes: the exact three-tool MCP surface from Task 2.
- Produces: client instructions that tell the primary agent to retrieve evidence, request a deterministic profile route, perform the review itself, and record one content-free task outcome.

- [ ] **Step 1: Write the failing documentation-artifact test**

```python
def test_operational_artifacts_describe_primary_agent_routing():
    artifacts = [
        ROOT / "README.md",
        ROOT / "docs/ROUTING_POLICY.md",
        ROOT / "client-rules/generic/QA_ROUTER_INSTRUCTIONS.md",
    ]

    for artifact in artifacts:
        text = artifact.read_text(encoding="utf-8").lower()
        assert "prepare_review_route" in text
        assert "qwen" not in text
        assert "lm studio" not in text
        assert "local delegation" not in text
```

- [ ] **Step 2: Run the focused test and verify the expected RED failure**

Run: `.venv/bin/pytest -q tests/test_install_artifacts.py::test_operational_artifacts_describe_primary_agent_routing`

Expected: FAIL because operational documentation still describes Qwen/local delegation and the old tool surface.

- [ ] **Step 3: Rewrite operational docs and remove obsolete artifacts**

Describe the deterministic route, primary-Codex ownership, optional host-owned `qa_deep`, three MCP tools, content-free metrics, and Python/uv-only setup. Remove obsolete local-routing skill, launch/runtime diagrams, and historical design/plan files that exist only for the retired model architecture. Do not add another model or agent runtime.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run: `.venv/bin/pytest -q tests/test_install_artifacts.py::test_operational_artifacts_describe_primary_agent_routing`

Expected: PASS.

- [ ] **Step 5: Commit documentation migration**

```bash
git add -A README.md CONTRIBUTING.md docs client-rules codex tests/test_install_artifacts.py
git commit -m "docs: describe primary-agent qa routing"
```

---

### Task 5: Full Verification and Final Review

**Files:**
- Modify: any files required by verification findings only.
- Test: complete repository suite and static checks.

**Interfaces:**
- Consumes: all completed tasks and the approved design spec.
- Produces: verified model-free repository state.

- [ ] **Step 1: Run the complete test suite**

Run: `.venv/bin/pytest -q`

Expected: zero failures and zero errors.

- [ ] **Step 2: Run Ruff**

Run: `.venv/bin/ruff check .`

Expected: `All checks passed`.

- [ ] **Step 3: Scan for retired operational references**

Run: `rg -n -i "qwen|lmstudio|lm studio|llmster|local model|local delegation|draft_review_checklist|record_canary_feedback|shadow_evaluation_required|generation_stats" --glob '!docs/superpowers/specs/2026-09-19-remove-local-model-runtime-design.md' --glob '!docs/superpowers/plans/2026-09-19-remove-local-model-runtime-plan.md' .`

Expected: no matches in source, operational docs, scripts, tests, lockfile, or launch artifacts.

- [ ] **Step 4: Run diff and status checks**

Run: `git diff --check && git status --short --branch`

Expected: no whitespace errors; only the planned commits are present and the worktree is clean after commits.

- [ ] **Step 5: Commit any verification-only fix and record evidence**

If verification finds a real regression, add a failing regression test first, apply one minimal fix, rerun the complete suite, and commit it. Otherwise leave the verified task commits unchanged and record the exact command results in the ledger.
