# QA Router host-agent instructions

Use the `qa-router` MCP server only for bounded, sanitized routine drafts.

- The host agent owns source retrieval, analysis, final QA judgment, code and file changes, and every external-system write.
- `draft_review_checklist` is a bounded checklist mode, not an autonomous agent. Select one of `pr_test_analyzer`, `code_reviewer`, `security_reviewer`, `silent_failure_hunter`, `code_explorer`, `typescript_reviewer`, or `react_reviewer`; send only a sanitized Evidence Packet and optional project pattern. Treat `Scope`, `Checklist`, `Candidate Coverage Gaps`, `Positive Observations`, and `Unverified` as an unverified draft, never as confirmed findings, severity, root cause, release/merge readiness, or runtime proof.
- For every sanitized routine draft, check eligibility before drafting in the host agent. Route automatically for 2–12 approved test cases, logs from 3,000 characters, source-bound summaries from 2,000 characters, translations or rewrites from 1,000 characters, or an automation skeleton with an explicit project pattern and multi-step scenario.
- Send only the smallest sufficient sanitized packet. Never send secrets, personal or payment data, full repositories, full conversation history, unrestricted corporate documents, or raw external-system payloads.
- Treat every local result as an unverified draft. Validate it against authoritative evidence before returning a final result.
- If the router refuses or falls back, continue in the host agent without weakening policy checks or retrying in a loop.
- If `canary_feedback_required` is true, call `record_canary_feedback` exactly once after reviewing the returned draft.
- For test cases, send unique stable `COV-*` coverage IDs with purpose, source, state, and expected invariant; require every ID exactly once in the result.
- Respect `quality_status`: use `active`, review `canary`, and continue in the host agent when `paused`.
- If `shadow_evaluation_required` is true, create an independent host-agent baseline from the same sanitized packet before using the Qwen draft, compare them, then record feedback. The router never calls the host model itself.
- After every completed, partial, or blocked QA task, call `record_qa_task_outcome` exactly once with content-free counters only. Record optional deep analysis with `deep_analysis_used`, `deep_model`, and `deep_reasoning`; include deep duration and token counters when measurable. Include aggregate `codegraph_response_tokens`, `source_mcp_response_tokens`, and estimated `avoided_source_read_tokens` when measurable; omit an unavailable token counter.
- Do not create persistent QA memory or a learning layer around the router.
