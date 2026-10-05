# QA Orchestrator
[![QA Orchestrator MCP server – quality and maintenance score on Glama](https://glama.ai/mcp/servers/Rbkmen/qa-orchestrator/badges/card.svg)](https://glama.ai/mcp/servers/Rbkmen/qa-orchestrator)

A deterministic MCP helper for host-owned QA reviews. The host keeps the code, evidence, model calls, findings, and final decision; the server keeps only bounded orchestration state.

## Example: a small Ruby guard change

Suppose a diff adds an early return to `BudgetGuard#check!` when the limit is absent.

**Ordinary QA:** follow project instructions, inspect the diff and targeted tests, then report confirmed findings.

**With QA Orchestrator:** the host does the same QA work; the server supplies a profile, checks reported transitions, and applies fixed escalation rules. It does not run the review.

```text
Host → start_qa_orchestration({"task_type":"ordinary_review"})
MCP  → {"run_id":"qar-7977ccfeb8ac487f9c83f756452cee1a","current_step":"triage","model_policy":{"mode":"host_default"}}
Host → inspect Ruby diff; choose ruby_reviewer; call prepare_qa_orchestration({"agent_profile":"ruby_reviewer"})
Host → advance_qa_orchestration({"run_id":"qar-7977ccfeb8ac487f9c83f756452cee1a","completed_step":"triage","status":"completed","selected_profile":"ruby_reviewer"})
MCP  → {"current_step":"primary_review","current_profile":"ruby_reviewer"}
Host → review against the returned checklist; then report completion and evidence-based signals
Host → advance_qa_orchestration({"run_id":"qar-7977ccfeb8ac487f9c83f756452cee1a","completed_step":"primary_review","status":"completed","completed_profile":"ruby_reviewer","risk_signals":{}})
MCP  → {"current_step":"synthesis"}
Host → advance_qa_orchestration({"run_id":"qar-7977ccfeb8ac487f9c83f756452cee1a","completed_step":"synthesis","status":"completed"})
MCP  → {"current_step":"awaiting_host_outcome"}
Host → finish_qa_orchestration({"run_id":"qar-7977ccfeb8ac487f9c83f756452cee1a","outcome":"completed"})
MCP  → {"status":"completed"}
```

Project instructions still guide the host. The orchestrator adds explicit session state and validates the sequence the host reports; it cannot confirm each review ran or that risk signals reflect evidence.

## Quick start

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), Git, and Codex CLI/Desktop. Replace `<commit-sha>` with the full hash of the revision you want to run:

```bash
codex mcp add qa-orchestrator -- uvx --from 'git+https://github.com/Rbkmen/qa-orchestrator.git@<commit-sha>' qa-orchestrator-mcp
```

This registers the server without a checkout or model-setup wizard. With no saved policy, `model_policy` is `host_default`: use the model and reasoning configured by the MCP host. Existing explicit policies remain active; see the installation guide to switch one back to host defaults.

The server sends concise advisory instructions during MCP initialization. The [canonical host rules](client-rules/generic/QA_ORCHESTRATOR_INSTRUCTIONS.md) define the full workflow and evidence contract. Execution speed and latency preferences remain controlled by the user's host/provider settings.

## Choose a review path

Use one profile for a routine, narrowly scoped, low-risk change. Use a fixed bundle for broad, cross-concern, or high-risk work. The host chooses from changed files and confirmed stack; `recommended_bundles` is a shortlist, not a risk score or restriction.

See the [bundle and profile catalog](docs/ORCHESTRATION_POLICY.md#fixed-bundles-and-profile-names), [deterministic deep-review rules](docs/ORCHESTRATION_POLICY.md#deterministic-deep-review-decision), and [complete MCP tool contract](docs/ORCHESTRATION_POLICY.md#mcp-tools).

## Responsibility boundary

The host owns evidence, model execution, review findings, severity, release readiness, and external actions. `QA Orchestrator` validates host-reported transitions and risk signals; it cannot verify that a profile ran or that a signal reflects evidence. `read_only=true` and `host_owns_decisions=true` describe the boundary; session tools still update local state. Use the updated session after `advance_qa_orchestration`; call `get_qa_orchestration` only to resume or recover.

When `model_policy.mode` is `configured`, the host uses its returned per-stage model settings. When it is `host_default`, the host uses its own configured model and reasoning. See [model policy and orchestration details](docs/ORCHESTRATION_POLICY.md#model-policy), the [installation guide](docs/INSTALLATION.md), and the [Codex and Claude Code guides](docs/clients/).

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md). Public MCP contract changes require focused tool-surface and ownership-boundary checks.
