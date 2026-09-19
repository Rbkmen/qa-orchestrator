# QA Router usage

Когда доступен MCP-сервер `qa-router`, используй его как детерминированный QA-routing helper.

- Получай evidence, запускай model stages, анализируй задачу, формируй findings, принимай финальное QA-решение и выполняй любые изменения или external writes в Claude Code.
- Для implementation-aware ревью вызови `start_qa_orchestration`, а затем `prepare_review_route` с наиболее узким профилем: `pr_test_analyzer`, `code_reviewer`, `security_reviewer`, `silent_failure_hunter`, `code_explorer`, `typescript_reviewer` или `react_reviewer`.
- Используй `gpt-5.6-luna/max` для triage, `gpt-5.6-terra/medium` для primary review и synthesis, optional `gpt-5.6-sol/high` для read-only deep analysis. После стадий передавай только structured signals через `advance_qa_orchestration`.
- `get_qa_orchestration` используй для чтения состояния и next action. Evidence, prompts и model outputs не передавай Router.
- Используй route только как focus/checklist. Сам проверь diff, callers, contracts, runtime evidence и unverified gaps.
- После `completed`, `partial` или `blocked` один раз вызови `record_qa_task_outcome` с counters без issue keys, source text, code, logs или paths.
- `get_metrics_report` используй только для aggregate read-only metrics.
- Не добавляй persistent QA memory, source cache или скрытые tool calls.
