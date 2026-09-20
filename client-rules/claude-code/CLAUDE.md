# QA Router usage

Когда доступен MCP-сервер `qa-router`, используй его как детерминированный QA-routing helper.

- Получай evidence, запускай model stages, анализируй задачу, формируй findings, принимай финальное QA-решение и выполняй любые изменения или external writes в Claude Code.
- Для implementation-aware ревью вызови `start_qa_orchestration`, на Luna выбери fixed bundle или compatibility profile, а затем `prepare_review_route` для каждой роли: `pr_test_analyzer`, `code_reviewer`, `security_reviewer`, `silent_failure_hunter`, `code_explorer`, `typescript_reviewer` или `react_reviewer`.
- Fixed bundles: `ordinary_mr`, `widget`, `security`, `autotest`, `requirements`; порядок приходит из Router и не изменяется host. `code_explorer` отображается как `Faraday — Evidence Investigator`; это внутреннее имя роли, не внешний сервис.
- Используй `gpt-5.6-luna/max` для triage, `gpt-5.6-terra/medium` для primary review каждой роли и synthesis, optional `gpt-5.6-sol/high` для read-only deep analysis. После каждой Terra-роли передавай её `completed_profile`; Sol и synthesis начинаются только после последней роли. Через `advance_qa_orchestration` передавай только structured signals.
- `get_qa_orchestration` используй для чтения состояния и next action. Evidence, prompts и model outputs не передавай Router.
- Используй route только как focus/checklist. Сам проверь diff, callers, contracts, runtime evidence и unverified gaps.
- После `completed`, `partial` или `blocked` один раз вызови `record_qa_task_outcome` с counters без issue keys, source text, code, logs или paths; для orchestration передай opaque `run_id`.
- `get_metrics_report` используй только для aggregate read-only metrics.
- Не добавляй persistent QA memory, source cache или скрытые tool calls.
