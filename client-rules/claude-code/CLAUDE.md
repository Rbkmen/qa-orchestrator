# QA Orchestrator usage

When the `qa-orchestrator` MCP server is available, use it as a deterministic QA-orchestration helper.

- Obtain evidence, run model stages, analyze the task, produce findings, make the final QA decision, and perform any changes or external writes in Claude Code.
- For an implementation-aware review, call `start_qa_orchestration`, select exactly one fixed bundle or compatibility profile during Luna triage (not both fields), and then call `prepare_review_route` for each role: `pr_test_analyzer`, `code_reviewer`, `security_reviewer`, `silent_failure_hunter`, `code_explorer`, `typescript_reviewer`, or `react_reviewer`.
- Fixed bundles are `ordinary_mr`, `widget`, `security`, `autotest`, and `requirements`; the order comes from the orchestrator and must not be changed by the host. `code_explorer` is displayed as `Faraday — Evidence Investigator`; this is an internal role name, not an external service.
- Use `gpt-5.6-luna/max`, `gpt-5.6-terra/medium`, and optional `gpt-5.6-sol/high` with `speed=1.0` for every stage. After each Terra role, pass its `completed_profile`; after the last role, pass only the boolean `risk_signals` object so the orchestrator applies the fixed deep-review rules. Sol and synthesis start only after the last role.
- For low-risk, narrow one-repository work, select one compatibility profile instead of a bundle; keep a compact Evidence Packet with stable `E1` references and bounded `F-01` candidates so roles do not repeat the full diff or logs.
- Use `get_qa_orchestration` to read state and the next action. Do not pass evidence, prompts, or model outputs to the orchestrator.
- Use the route only as a focus/checklist. Independently verify the diff, callers, contracts, runtime evidence, and unverified gaps.
- After `completed`, `partial`, or `blocked`, call `record_qa_task_outcome` once with counters and no issue keys, source text, code, logs, or paths; for orchestration, pass the opaque `run_id`.
- Use `get_metrics_report` only for aggregate read-only metrics.
- Do not add persistent QA memory, a source cache, or hidden tool calls.
