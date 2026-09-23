# QA Orchestrator usage

The generic QA Orchestrator instructions are canonical for orchestration mechanics; workspace and repository rules add only scope-specific routing and output requirements.

When the `qa-orchestrator` MCP server is available, use it as a deterministic QA-orchestration helper.

- If the MCP is unavailable, a required call fails, or the session expires,
  continue with the selected workspace and repository rules, mark orchestration
  and task-distribution recording as unavailable, and never emulate or claim an orchestration call.
- A partial result caused by missing orchestration input remains a valid
  host-owned QA result; report the missing boundary.
- Obtain evidence, run model stages, analyze the task, produce findings, make the final QA decision, and perform any changes or external writes in Claude Code.
- Keep orchestration read-only: preserve `read_only=true` and `host_owns_decisions=true`; Claude Code owns evidence, decisions, and external writes.
- For an implementation-aware review, call `start_qa_orchestration`, select exactly one fixed bundle or compatibility profile during triage (not both fields), and then call `prepare_review_route` for each role: `pr_test_analyzer`, `code_reviewer`, `security_reviewer`, `silent_failure_hunter`, `code_explorer`, `typescript_reviewer`, or `react_reviewer`.
- Fixed bundles are `ordinary_mr`, `widget`, `security`, `autotest`, and `requirements`; the order comes from the orchestrator and must not be changed by the host. `code_explorer` is displayed as `Faraday — Evidence Investigator`; this is an internal role name, not an external service.
- Use the returned `model_policy` for every stage; keep `speed=1.0`. The default provider is OpenAI/Codex, and `qa-orch setup` can select OpenAI or Anthropic, model IDs, and provider-specific reasoning/effort independently of the stage names. OpenAI uses `reasoning.effort`; Anthropic uses `output_config.effort`; `none` means omit the provider parameter. The wizard does not contact provider APIs; verify model access and custom-ID reasoning/effort support with the host provider. After each primary-review role, pass its `completed_profile`; after the last role, you must also pass the structured boolean `risk_signals` object (use `{}` when none apply) so the orchestrator applies the fixed deep-review rules. Deep review and synthesis start only after the last role. Keep stage labels and status text model-neutral; use function names only, without model names or reasoning levels. Transition identifiers are model-neutral; use settings from `model_policy`, not from the transition name.
- For low-risk, narrow one-repository work, select one compatibility profile instead of a bundle; keep a compact Evidence Packet with stable `E1` references and bounded `F-01` candidates so roles do not repeat the full diff or logs.
- Use `get_qa_orchestration` to read state and the next action. Do not pass evidence, prompts, or model outputs to the orchestrator.
- Use the route only as a focus/checklist. Independently verify the diff, callers, contracts, runtime evidence, and unverified gaps.
- When the current QA task reaches its final reported status (`completed`,
  `partial`, or `blocked`), call `record_qa_task_outcome` exactly once with
  only `task_type`, `outcome`, and the opaque `run_id` when orchestration was
  used. Do not record intermediate continuations; if the task resumes after a
  partial result, record only the final status. The outcome finalizes an
  orchestrated session but is not saved in distribution data. Only task type
  and timestamp are stored; findings, model settings, stage calls, and source
  calls are not collected.
- Use `get_metrics_report` only for the aggregate task distribution: total
  tasks and counts by task type.
- For implementation-aware reviews, use Findings, Changes, Manual Test Plan, and Open Questions / Could Not Verify; follow the workspace flow when it defines another output shape for requirements or planning.
- Do not add persistent QA memory, a source cache, or hidden tool calls.
