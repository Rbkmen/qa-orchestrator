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
- For an implementation-aware review, call `start_qa_orchestration`, select exactly one fixed bundle or compatibility profile during triage (not both fields), and then call `prepare_review_route` for each role: `pr_test_analyzer`, `code_reviewer`, `security_reviewer`, `silent_failure_hunter`, `code_explorer`, `typescript_reviewer`, `react_reviewer`, `ruby_reviewer`, `python_reviewer`, or `mobile_reviewer`.
- Fixed bundles are `ordinary_mr`, `widget`, `widget_js`, `ruby_backend`, `python_backend`, `mobile`, `security`, `autotest`, and `requirements`; the order comes from the orchestrator and must not be changed by the host. `code_explorer` is displayed as `Faraday — Evidence Investigator`; this is an internal role name, not an external service.
- `autotest` is for broad TypeScript automation changes; use `ordinary_mr` for broad non-TypeScript automation. `widget` is for TypeScript React changes; use `widget_js` for broad JavaScript React changes; use `ruby_backend` for broad Ruby backend changes, `python_backend` for broad Python/MCP work, and `mobile` for broad React Native/native iOS/Android work. For narrow test-only, React-only, Ruby-only, Python/MCP-only, or mobile-platform-only work, use the corresponding `pr_test_analyzer`, `react_reviewer`, `ruby_reviewer`, `python_reviewer`, or `mobile_reviewer` profile. Choose using changed paths and confirmed manifests, not the repository name alone.
- Use the returned `model_policy` for every stage. Execution speed and latency preferences are controlled by the user's host/provider settings; the orchestrator does not set or override them. The default provider is OpenAI/Codex, and `qa-orch setup` can select OpenAI or Anthropic, model IDs, and provider-specific reasoning/effort independently of the stage names. OpenAI uses `reasoning.effort`; Anthropic uses `output_config.effort`; `none` means omit the provider parameter. The wizard does not contact provider APIs; verify model access and custom-ID reasoning/effort support with the host provider. After each primary-review role, pass its `completed_profile`; after the last role, you must also pass the structured boolean `risk_signals` object (use `{}` when none apply) so the orchestrator applies the fixed deep-review rules. Deep review and synthesis start only after the last role. Keep stage labels and status text model-neutral; use function names only, without model names or reasoning levels. Transition identifiers are model-neutral; use settings from `model_policy`, not from the transition name.
- For low-risk, narrow one-repository work, select one compatibility profile instead of a bundle; keep a compact Evidence Packet with stable `E1` references and bounded `F-01` candidates so roles do not repeat the full diff or logs.
- Use `get_qa_orchestration` to read state and the next action. Do not pass evidence, prompts, or model outputs to the orchestrator.
- Use the route only as a focus/checklist. Independently verify the diff, callers, contracts, runtime evidence, and unverified gaps.
- Follow each route's `required_sections`. `code_explorer` returns an evidence map and unresolved links, not defect candidates; `code_reviewer` uses that map and reports concrete candidates supported by changed code and evidence. Keep coverage gaps separate from possible defects, and omit empty boilerplate.
- On the final primary-review role, set each `risk_signals` boolean only when the evidence supports it: sensitive identity/security/payment/fraud/privacy/access-control impact → `high_risk_domain`; a material missing source → `evidence_uncertain`; a relevant cross-repository/service/system boundary → `cross_system_scope`; multiple plausible causes left after investigation → `multiple_plausible_causes`; conflicting authoritative evidence → `evidence_conflict`; expected behavior that cannot be reproduced → `non_reproducible`; broad impact across consumers/data → `high_blast_radius`. A profile escalation note or ordinary coverage gap alone does not trigger deep review.
- After synthesis, call `finish_qa_orchestration` once with the session
  `run_id` and the host-owned final outcome (`completed`, `partial`, or
  `blocked`). For an early stop, pass the same `partial` or `blocked` outcome
  already sent to `advance_qa_orchestration`.
- The call finalizes only the in-memory session. Do not call it for a task that
  was not orchestrated. Repeating the same outcome is idempotent; a conflicting
  final outcome is rejected.
- For implementation-aware reviews, use Findings, Changes, Manual Test Plan, and Open Questions / Could Not Verify; follow the workspace flow when it defines another output shape for requirements or planning.
- Do not add persistent QA memory, a source cache, or hidden tool calls.
