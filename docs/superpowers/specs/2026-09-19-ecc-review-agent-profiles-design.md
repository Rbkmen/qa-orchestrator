# QA Router ECC review-agent profiles

## Intent

Add a bounded, read-only review-lane interface to QA Router based on the
useful QA parts of the ECC agents. The local Qwen model may draft review
checklists, candidate coverage gaps, candidate questions, and positive
observations from a sanitized Evidence Packet. The host agent remains the
owner of source retrieval, evidence validation, findings, severity, root
cause, release/merge decisions, code changes, and external-system writes.

## In scope

The router exposes seven named profiles:

- `pr_test_analyzer`
- `code_reviewer`
- `security_reviewer`
- `silent_failure_hunter`
- `code_explorer`
- `typescript_reviewer`
- `react_reviewer`

The first four are the recommended QA lanes. The last three are bounded
checklist variants for repository and frontend work. ECC-specific model
selection, autonomous hooks, memory, continuous learning, external MCPs,
and write-capable tools are not imported.

## Interface

Add one MCP tool, `draft_review_checklist`:

- `agent_profile`: one of the seven profile identifiers;
- `evidence_packet`: sanitized, bounded source-bound input;
- `project_pattern`: optional existing project pattern or test convention.

The result reuses the standard `DraftEnvelope` and is explicitly an
unverified local draft. The prompt requires these sections in the draft:

- `Scope`
- `Checklist`
- `Candidate Coverage Gaps`
- `Positive Observations`
- `Unverified`

The model must not decide severity, priority, root cause, release readiness,
merge readiness, or whether an issue is confirmed. It must not invent files,
line numbers, contracts, runtime results, or external-system state. The host
agent validates and rewrites the result before using it.

## Safety and routing

- Reuse the existing sanitization, secret/PII/payment detection, decision
  refusal, token budget, quality gate, canary feedback, shadow evaluation,
  and fallback behavior.
- Add a separate draft kind so metrics distinguish review-lane drafts from
  ordinary summaries and test-case drafts.
- Keep the local model pinned to `qwen/qwen3.5-9b`, context at 16,384, and
  parallelism at one.
- Do not add persistent memory, agent-to-agent calls, model switching,
  external MCP calls, file writes, Git operations, or Jira/GitLab/TestRail
  operations.
- The profile registry is static and code-owned; user-supplied profile names
  cannot alter instructions or permissions.

## Files expected to change

- `src/qa_router_mcp/contracts.py`
- `src/qa_router_mcp/config.py`
- `src/qa_router_mcp/prompts.py`
- `src/qa_router_mcp/validation.py`
- `src/qa_router_mcp/service.py`
- `src/qa_router_mcp/server.py`
- `tests/test_config.py`
- `tests/test_prompts.py`
- `tests/test_validation.py`
- `tests/test_service.py`
- `tests/test_server.py`
- `docs/ROUTING_POLICY.md`
- `client-rules/generic/QA_ROUTER_INSTRUCTIONS.md`

## Verification criteria

1. Unknown profiles fail before backend tokenization or generation.
2. Every profile produces its own immutable prompt markers and cannot
   override the system safety prompt.
3. Review-lane requests containing secrets, sensitive identifiers, or
   decision-seeking language are refused by the existing policy.
4. A valid review-lane draft receives the same quality-gate and feedback
   metadata as other local drafts.
5. The MCP schema exposes the new tool without changing existing tool
   behavior.
6. Output validation rejects external write instructions and unsupported
   decision claims where they can be detected structurally.
7. The full repository test suite and Ruff checks pass, or any pre-existing
   environment failure is reported explicitly.

## Non-goals

- This change does not create autonomous review agents.
- This change does not replace CodeGraph, the host agent, `qa_deep`, WDIO /
  Appium, TestRail, Stage testing, Sentry, Grafana, OpenSearch, or release
  verification.
- This change does not install or copy the ECC repository.
