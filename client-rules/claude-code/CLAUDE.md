# QA Orchestrator usage

The generic QA Orchestrator instructions are canonical for orchestration mechanics; workspace and repository rules add only scope-specific routing and output requirements.

When the `qa-orchestrator` MCP server is available, use it as a deterministic QA-orchestration helper.

  - If the MCP is unavailable, a required call fails, or the session expires,
  continue with the selected workspace and repository rules, mark orchestration
  as unavailable, and never emulate or claim an orchestration call.
- A partial result caused by missing orchestration input remains a valid
  host-owned QA result; report the missing boundary.
- Obtain evidence, run model stages, analyze the task, produce findings, make the final QA decision, and perform any changes or external writes in Claude Code.
- Keep orchestration read-only: preserve `read_only=true` and `host_owns_decisions=true`; Claude Code owns evidence, decisions, and external writes.
- For an implementation-aware review, call `start_qa_orchestration`, select exactly one fixed bundle or compatibility profile during triage (not both fields), and then call `prepare_qa_orchestration` for each role: `pr_test_analyzer`, `code_reviewer`, `security_reviewer`, `silent_failure_hunter`, `code_explorer`, `typescript_reviewer`, `react_reviewer`, `ruby_reviewer`, `python_reviewer`, or `mobile_reviewer`.
- Treat returned `recommended_bundles` as a task-type-based shortlist only; `allowed_bundles` remains the complete set. Choose using the actual changed paths and confirmed project stack.
- When profiles, bundle purposes, or route order are unclear before triage, call `get_qa_orchestration_catalog` with no arguments. It returns the fixed catalog and task-based shortlists without creating a session or changing state, TTL, or storage. Use `prepare_qa_orchestration` for one chosen profile's detailed checklist; apply a chosen route with `advance_qa_orchestration` at triage.
- Fixed bundles are `ordinary_mr`, `widget`, `widget_js`, `ruby_backend`, `python_backend`, `mobile`, `security`, `autotest`, and `requirements`; the order comes from the orchestrator and must not be changed by the host. `code_explorer` is displayed as `Faraday — Evidence Investigator`; this is an internal role name, not an external service.
- `autotest` is for broad TypeScript automation changes; use `ordinary_mr` for broad non-TypeScript automation. `widget` is for TypeScript React changes; use `widget_js` for broad JavaScript React changes; use `ruby_backend` for broad Ruby backend changes, `python_backend` for broad Python/MCP work, and `mobile` for broad React Native/native iOS/Android work. For narrow test-only, React-only, Ruby-only, Python/MCP-only, or mobile-platform-only work, use the corresponding `pr_test_analyzer`, `react_reviewer`, `ruby_reviewer`, `python_reviewer`, or `mobile_reviewer` profile. Choose using changed paths and confirmed manifests, not the repository name alone.
- Use the returned `model_policy` for every stage. `reasoning_capabilities_verified` is true only for an exact locally curated model/provider pair; false means verify the selected effort with the provider. The flag does not check live model access or capabilities. Execution speed and latency preferences are controlled by the user's host/provider settings; the orchestrator does not set or override them. The default provider is OpenAI/Codex, and `qa-orch setup` can select OpenAI or Anthropic, model IDs, and provider-specific reasoning/effort independently of the stage names. OpenAI uses `reasoning.effort`; Anthropic uses `output_config.effort`; `none` disables reasoning on OpenAI reasoning models that support it; omit the parameter for non-reasoning models such as GPT-4.1. For Anthropic, `none` is a local sentinel to omit effort and use model defaults. The wizard does not contact provider APIs; verify model access and custom-ID reasoning/effort support with the host provider. After each primary-review role, pass its `completed_profile`; after the last role, you must also pass the structured boolean `risk_signals` object (use `{}` when none apply) so the orchestrator applies the fixed deep-review rules. Deep review and synthesis start only after the last role. Keep stage labels and status text model-neutral; use function names only, without model names or reasoning levels. Transition identifiers are model-neutral; use settings from `model_policy`, not from the transition name.
- For low-risk, narrow one-repository work, select one compatibility profile instead of a bundle; keep a compact Evidence Packet with stable `E1` references and bounded `F-01` candidates so roles do not repeat the full diff or logs.
- Use the updated session returned by `advance_qa_orchestration` for the next action and model policy. Call `get_qa_orchestration` only to resume or recover an interrupted or unclear session. Do not pass evidence, prompts, or model outputs to the orchestrator.
- If `run_id` is lost, call `list_qa_orchestrations` with no arguments, identify the intended entry by task type, route, stage, and expiry, then call `get_qa_orchestration` with its ID. If several entries match, ask which session to resume. Listing does not renew TTL or write storage, and cannot recover expired or evicted sessions. After restart, only unfinished sessions restored from configured local storage are available.
- Use the route only as a focus/checklist. Independently verify the diff, callers, contracts, runtime evidence, and unverified gaps.
- Follow each route's `required_sections`. `code_explorer` returns an evidence map and unresolved links, not defect candidates; `code_reviewer` uses that map and reports concrete candidates supported by changed code and evidence. Keep coverage gaps separate from possible defects, and omit empty boilerplate.
- On the final primary-review role, set each `risk_signals` boolean only when the evidence supports it: sensitive identity/security/payment/fraud/privacy/access-control impact → `high_risk_domain`; a material missing source → `evidence_uncertain`; a relevant cross-repository/service/system boundary → `cross_system_scope`; multiple plausible causes left after investigation → `multiple_plausible_causes`; conflicting authoritative evidence → `evidence_conflict`; expected behavior that cannot be reproduced → `non_reproducible`; broad impact across consumers/data → `high_blast_radius`. A profile escalation note or ordinary coverage gap alone does not trigger deep review.
- After synthesis, call `finish_qa_orchestration` once with the session
  `run_id` and the host-owned final outcome (`completed`, `partial`, or
  `blocked`). For an early stop, pass the same `partial` or `blocked` outcome
  already sent to `advance_qa_orchestration`.
- By default, session state stays in memory. If `QA_ORCHESTRATOR_SESSION_STORE_PATH`
  is configured, unfinished structured state can be recovered after a restart;
  finalization deletes its stored row. Final outcomes are not persisted. Do not
  call this for a task that was not orchestrated. Repeating the same outcome is
  idempotent in the current process; a conflicting final outcome is rejected.
- For implementation-aware reviews, use Findings, Changes, Manual Test Plan, and Open Questions / Could Not Verify; follow the workspace flow when it defines another output shape for requirements or planning.
- Do not add persistent QA memory, a source cache, or hidden tool calls.
- When asked which models are loaded, call `get_qa_orchestration_model_policy`
  with no arguments. It returns the current server's policy for new sessions,
  without reading disk or creating a session. After setup, restart the server
  connection before checking it. CLI `config show` and `reload` read disk and
  do not reload the connected process. Check actual execution in the host.
  For a recovered session, follow its returned `model_policy`; its next
  transition uses the restarted server's policy.
