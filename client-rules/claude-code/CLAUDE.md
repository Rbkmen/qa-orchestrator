# QA Router usage

Когда доступен MCP-сервер `qa-router`, используй его как детерминированный QA-routing helper.

- Получай evidence, анализируй задачу, формируй findings, принимай финальное QA-решение и выполняй любые изменения или external writes в Claude Code.
- Для implementation-aware ревью один раз вызови `prepare_review_route` с наиболее узким профилем: `pr_test_analyzer`, `code_reviewer`, `security_reviewer`, `silent_failure_hunter`, `code_explorer`, `typescript_reviewer` или `react_reviewer`.
- Используй route только как focus/checklist. Сам проверь diff, callers, contracts, runtime evidence и unverified gaps.
- При сложном cross-repository, debugging или security-sensitive анализе можно использовать одну bounded read-only `qa_deep` эскалацию в host client. Router не запускает её и не принимает за тебя решение.
- После `completed`, `partial` или `blocked` один раз вызови `record_qa_task_outcome` с counters без issue keys, source text, code, logs или paths.
- `get_metrics_report` используй только для aggregate read-only metrics.
- Не добавляй persistent QA memory, source cache или скрытые tool calls.
