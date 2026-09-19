# QA Router host-agent instructions

Используй `qa-router` как детерминированный helper для QA-профиля и обезличенных task metrics. Primary host agent остаётся единственным владельцем evidence, анализа, решений и внешних действий.

## Review routing

- Для implementation-aware QA выбери один профиль и вызови `prepare_review_route`:
  `pr_test_analyzer`, `code_reviewer`, `security_reviewer`, `silent_failure_hunter`, `code_explorer`, `typescript_reviewer` или `react_reviewer`.
- Используй возвращённые `focus`, `required_sections`, `constraints` и `escalation_signals` как рабочий checklist.
- Сам получи Jira/MR/TestRail/monitoring/code evidence, проверь diff и отдели confirmed findings от hypotheses и unverified runtime/release facts.
- Всегда сохраняй итоговый формат: Findings, Changes, Manual Test Plan, Open Questions / Could Not Verify.
- Route помечен `read_only=true` и `host_owns_decisions=true`; не трактуй его как автономного агента и не делегируй ему external writes.

## Optional deep analysis

Для трудного cross-repository reasoning, debugging, security/payment/fraud-sensitive анализа или high-blast-radius edge cases host может использовать одну bounded read-only `qa_deep` эскалацию. Передай ей только минимальный Evidence Packet, проверь ответ самостоятельно и не создавай вторую эскалацию автоматически.

## Metrics

- После каждого `completed`, `partial` или `blocked` QA task один раз вызови `record_qa_task_outcome`.
- Передавай только `task_type`, `outcome`, counters вызовов, findings, repeated reads и измеримые `deep_*`/token counters.
- Никогда не передавай issue keys, titles, paths, source text, code, logs, screenshots, prompts или ответы ревью.
- `get_metrics_report(days)` используй только для агрегированного read-only отчёта.

Не добавляй persistent QA memory, source cache, learning layer или скрытые вызовы инструментов.
