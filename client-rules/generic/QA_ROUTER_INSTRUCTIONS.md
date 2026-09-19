# QA Router host-agent instructions

Используй `qa-router` как детерминированный helper для QA-профиля и обезличенных task metrics. Primary host agent остаётся единственным владельцем evidence, анализа, решений и внешних действий.

## Review routing

- Для implementation-aware QA начни с `start_qa_orchestration`. На Luna выбери один fixed bundle или один compatibility profile и вызови `prepare_review_route` для каждого выбранного профиля:
  `pr_test_analyzer`, `code_reviewer`, `security_reviewer`, `silent_failure_hunter`, `code_explorer`, `typescript_reviewer` или `react_reviewer`.
- Доступные bundles: `ordinary_mr` (`code_explorer` → `code_reviewer` → `pr_test_analyzer`), `widget` (`code_explorer` → `react_reviewer` → `typescript_reviewer` → `pr_test_analyzer`), `security` (`code_explorer` → `security_reviewer` → `silent_failure_hunter`), `autotest` (`code_reviewer` → `pr_test_analyzer` → `typescript_reviewer`) и `requirements` (`code_explorer` → `code_reviewer`). Не меняй порядок и не передавай собственный список.
- Отображаемые имена ролей: `Faraday — Evidence Investigator` = `code_explorer`, `Code Reviewer`, `Test Analyzer`, `Security Reviewer`, `Silent Failure Hunter`, `TypeScript Reviewer`, `React Reviewer`. Faraday — внутреннее имя роли, не внешний сервис или отдельная model.
- Используй возвращённые `focus`, `required_sections`, `constraints` и `escalation_signals` как рабочий checklist.
- Выполняй stages по policy: `gpt-5.6-luna/max` для triage, `gpt-5.6-terra/medium` для primary review каждой роли в порядке bundle и synthesis, optional `gpt-5.6-sol/high` для read-only deep analysis. Показывай host status: `Luna / Max → Ordinary MR Review` → `Terra / Medium → Faraday — Evidence Investigator` → `Terra / Medium → Code Reviewer` → `Terra / Medium → Test Analyzer` → `Terra / Medium → Synthesis` → `Host → Final QA outcome`; при escalation добавляй `Sol / High → Deep read-only review`. После каждой стадии вызывай `advance_qa_orchestration` с structured signal, а `get_qa_orchestration` используй для next action.
- Сам получи Jira/MR/TestRail/monitoring/code evidence, проверь diff и отдели confirmed findings от hypotheses и unverified runtime/release facts.
- Всегда сохраняй итоговый формат: Findings, Changes, Manual Test Plan, Open Questions / Could Not Verify.
- Route и orchestration states помечены `read_only=true` и `host_owns_decisions=true`; не трактуй Router как автономного агента и не делегируй ему external writes.

## Optional deep analysis

Для трудного cross-repository reasoning, debugging, security/payment/fraud-sensitive анализа или high-blast-radius edge cases host может запросить optional Sol/high escalation фиксированным `reason_code`. Проверь ответ самостоятельно и не создавай вторую эскалацию автоматически.

## Metrics

- После каждого `completed`, `partial` или `blocked` QA task один раз вызови `record_qa_task_outcome`.
- Передавай только `task_type`, `outcome`, counters вызовов, findings, repeated reads и измеримые `deep_*`/token counters.
- Никогда не передавай issue keys, titles, paths, source text, code, logs, screenshots, prompts или ответы ревью.
- `get_metrics_report(days)` используй только для агрегированного read-only отчёта.

Router публикует ровно шесть tools: `prepare_review_route`, `start_qa_orchestration`, `advance_qa_orchestration`, `get_qa_orchestration`, `record_qa_task_outcome`, `get_metrics_report`.

Не добавляй persistent QA memory, source cache, learning layer или скрытые вызовы инструментов.
