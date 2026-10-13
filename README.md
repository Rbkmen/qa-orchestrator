# QA Orchestrator
[![QA Orchestrator MCP server – quality and maintenance score on Glama](https://glama.ai/mcp/servers/Rbkmen/qa-orchestrator/badges/card.svg)](https://glama.ai/mcp/servers/Rbkmen/qa-orchestrator)

A deterministic MCP helper for host-owned QA reviews. The host owns the code, evidence, model calls, and final decision. When a review is bound to an immutable input, the server can retain its source identifiers and structured reports; it does not store the raw diff or full Evidence Packet.

## Example: a small Ruby guard change

Suppose a diff adds an early return to `BudgetGuard#check!` when the limit is absent.

**Ordinary QA:** follow project instructions, inspect the diff and targeted tests, then report confirmed findings.

**With QA Orchestrator:** the host does the same QA work; the server supplies a profile, checks reported transitions, and applies fixed escalation rules. It does not run the review.

```text
Host → start_qa_orchestration({"task_type":"ordinary_review"})
MCP  → {"run_id":"qar-7977ccfeb8ac487f9c83f756452cee1a","current_step":"triage","model_policy":{"mode":"host_default"}}
Host → inspect Ruby diff; choose ruby_reviewer
Host → advance_qa_orchestration({"run_id":"qar-7977ccfeb8ac487f9c83f756452cee1a","completed_step":"triage","status":"completed","selected_profile":"ruby_reviewer"})
MCP  → primary_review; selected_review_routes returns the checklist and execution_groups=[["ruby_reviewer"]]
Host → review against the returned checklist; then report completion and evidence-based signals
Host → advance_qa_orchestration({"run_id":"qar-7977ccfeb8ac487f9c83f756452cee1a","completed_step":"primary_review","status":"completed","completed_profiles":["ruby_reviewer"],"risk_signals":{}})
MCP  → {"current_step":"synthesis"}
Host → advance_qa_orchestration({"run_id":"qar-7977ccfeb8ac487f9c83f756452cee1a","completed_step":"synthesis","status":"completed"})
MCP  → {"current_step":"awaiting_host_outcome"}
Host → finish_qa_orchestration({"run_id":"qar-7977ccfeb8ac487f9c83f756452cee1a","outcome":"completed"})
MCP  → {"status":"completed"}
```

This short transcript omits the optional audit binding for readability. For a reproducible review record, start with `review_context` (repository, base/head SHAs, diff and Evidence Packet SHA-256 digests, changed paths, and evidence IDs), submit each profile's `profile_reports`, then pass the synthesized `final_review_report` when finalizing. See [bound review artifacts](docs/ORCHESTRATION_POLICY.md#bound-and-retained-review-artifacts) for the fields and retention behavior.

Project instructions still guide the host. The orchestrator adds explicit session state and validates the sequence the host reports; it cannot confirm each review ran or that risk signals reflect evidence.

## Quick start

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), Git, and Codex CLI/Desktop. Quick Start uses the reviewed server revision:

```bash
codex mcp add qa-orchestrator -- uvx --from 'git+https://github.com/Rbkmen/qa-orchestrator.git@9c278d8be0362e79de39e1a3e530a96fab48e414' qa-orchestrator-mcp
```

This registers the server without a checkout or model-setup wizard. With no saved policy, `model_policy` is `host_default`: use the model and reasoning configured by the MCP host. Existing explicit policies remain active; see the installation guide to switch one back to host defaults.

The server sends concise advisory instructions during MCP initialization. The [canonical host rules](client-rules/generic/QA_ORCHESTRATOR_INSTRUCTIONS.md) define the full workflow and evidence contract. Execution speed and latency preferences remain controlled by the user's host/provider settings.

## Choose a review path

Use one profile for a routine, narrowly scoped, low-risk change. Several changed files alone do not require a bundle. Use a fixed bundle for broad, cross-concern, or high-risk work. After selecting a route during triage, use `selected_review_routes` from the `advance_qa_orchestration` response; it contains the checklists and `execution_groups`, so the session needs no separate preparation call. The stateless `prepare_qa_orchestration` tools remain available for standalone lookup or recovery. Profiles in one group may run concurrently; wait for the group, then record it once with `completed_profiles` in fixed order. Older clients can still report profiles individually with `completed_profile`. Use sequential execution when the host cannot run workers. The host chooses from changed files and confirmed stack; `recommended_bundles` is a shortlist, not a risk score or restriction.

For a single-profile review only, set `skip_synthesis=true` on the final primary-review transition when the profile reports no findings, no unresolved material questions, and all risk signals are false. This skips only the separate synthesis step: the host still validates and produces the final QA result. Keep synthesis for bundles and any review with findings, unresolved questions, or risk signals.

See the [bundle and profile catalog](docs/ORCHESTRATION_POLICY.md#fixed-bundles-and-profile-names), [deterministic deep-review rules](docs/ORCHESTRATION_POLICY.md#deterministic-deep-review-decision), and [complete MCP tool contract](docs/ORCHESTRATION_POLICY.md#mcp-tools).

## Responsibility boundary

The host owns evidence, model execution, finding confirmation, severity, release readiness, and external actions. For a bound review, `QA Orchestrator` checks report/profile alignment and that citations use registered Evidence Packet references; it cannot verify that a profile ran or that a report reflects its sources. `read_only=true` and `host_owns_decisions=true` describe the boundary; session tools still update local state. Use the updated session after `advance_qa_orchestration`; call `get_qa_orchestration` to resume, recover, or read saved reports.

When `model_policy.mode` is `configured`, the host uses its returned per-stage model settings. When it is `host_default`, the host uses its own configured model and reasoning. See [model policy and orchestration details](docs/ORCHESTRATION_POLICY.md#model-policy), the [installation guide](docs/INSTALLATION.md), and the [Codex and Claude Code guides](docs/clients/).

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md). Public MCP contract changes require focused tool-surface and ownership-boundary checks.
