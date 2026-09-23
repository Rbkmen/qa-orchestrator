# GPT-6 Model Policy and Metrics Compatibility

**Status:** Approved by user on 2026-09-22

## Goal

Move the host-owned QA Orchestrator policy from GPT-5.6 to GPT-6 without
mislabeling model usage, breaking existing metrics history, or changing the
orchestrator into a model runtime. The host still runs every model stage and
owns evidence, findings, decisions, and external actions.

## Model policy

| Stage | Model | Reasoning | Responsibility |
|---|---|---|---|
| Triage | `gpt-6-luna` | `max` | Select one fixed review bundle or compatibility profile and identify evidence gaps |
| Primary review | `gpt-6-sol` | `medium` | Review each selected profile in the existing fixed order |
| Deep escalation | `gpt-6-sol` | `high` | Optional, separate read-only pass for a complex or high-risk case |
| Synthesis | `gpt-6-sol` | `medium` | Consolidate the host-validated result |

All stages retain `speed=1.0`. Deep review remains optional and gated by the
existing deterministic risk signals. There is no automatic fallback, new model
runtime, or GPT-6 Astra stage in this change.

## Metrics event compatibility

New metric events use `schema_version=2` and count work by orchestration stage,
not by model family:

- `triage_calls`;
- `primary_review_calls`;
- `deep_review_calls`;
- `synthesis_calls`;
- existing `orchestration_steps_completed` and `orchestration_retries`.

New token fields follow the same stage names: `triage_input_tokens`,
`triage_output_tokens`, `primary_review_input_tokens`,
`primary_review_output_tokens`, `synthesis_input_tokens`, and
`synthesis_output_tokens`. Existing `deep_input_tokens` and
`deep_output_tokens` remain specific to the optional deep pass.

For completed orchestrated bundles with `N` profiles, v2 requires at least one
triage call, `N` primary-review calls, one synthesis call, and `N + 2` completed
steps. When deep review runs, `deep_review_calls` must be positive and the
event adds one completed step; record the actual number of deep-review model
calls. The recorded model and reasoning are `gpt-6-sol` and `high`.

The v1 event schema and existing JSONL rows remain unchanged and readable.
Validation branches by event schema: v1 keeps its current model-named
invariants; v2 validates stage-named counters. No historical event is rewritten
or relabeled as GPT-6.

## Metrics report compatibility

Keep current legacy report fields available for v1 data and add explicit v2
stage summaries (`stage_calls` and `stage_tokens`). Do not map v2 Sol usage into
legacy `terra_calls` or `sol_calls`: those fields have different meanings and
`sol_calls` currently signals deep review. Aggregate orchestration task totals
across both schemas, but keep legacy model counters and v2 stage counters
separate. `deep_by_model` continues to report exact model IDs so 5.6 and GPT-6
history remain distinguishable.

Model-token measurement coverage treats a v1 event as complete when its current
v1 token fields are present, and a v2 event as complete when its v2 stage-token
fields are present. The report response adds the new summaries without removing
the existing legacy keys.

## Client and documentation updates

Update the canonical QA Orchestrator instructions and Codex/Claude client
rules, current README and orchestration policy, MCP metric input, and tests to
use the GPT-6 policy and v2 stage fields. Preserve dated design/plan documents
as records of the earlier GPT-5.6 policy rather than rewriting history. Keep
the six-tool MCP surface unchanged.

## Non-goals

- Calling GPT models from the QA Orchestrator process;
- changing review bundles, profile ordering, escalation thresholds, or final QA
  ownership;
- automatic model fallback or retries;
- rewriting, deleting, or relabeling stored v1 metrics;
- adding GPT-6 Astra or a new client/runtime integration.

## Acceptance checks

1. New orchestration sessions return the exact model/reasoning mapping above.
2. v2 validation rejects mismatched stage counts and deep-review metadata.
3. v1 and v2 events are both accepted by the metrics report, with separate
   counters and correct model attribution.
4. Existing clients' current instructions point to GPT-6 and stage-based
   metrics; dated historical design documents remain intact.
5. The MCP tool count remains six and all tests/lint checks pass.
