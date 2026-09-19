# QA Router Policy

Эта policy client-neutral. **Host agent** — Codex, Claude Code, Cursor или другой MCP-клиент — остаётся главным оркестратором и владельцем решений.

## Responsibility boundary

Host agent owns:

- task classification и получение authoritative sources;
- requirements, diff, code, contract, log и runtime analysis;
- findings, severity, coverage, release/readiness judgment и финальный ответ;
- CodeGraph и source-MCP calls;
- code/file changes и все записи в Jira, GitLab, TestRail, Sentry или другие системы;
- optional `qa_deep` и проверку его результата.

QA Router owns only deterministic profile routing and content-free metrics. Профили не являются autonomous agents: они не читают sources, не вызывают другие tools, не создают threads, не пишут файлы и не публикуют findings.

## Review flow

1. Host классифицирует QA-задачу и собирает минимально необходимое evidence.
2. Host вызывает `prepare_review_route(agent_profile)` с одним стабильным профилем.
3. Router возвращает `focus`, `required_sections`, `constraints`, `escalation_signals`, `read_only=true` и `host_owns_decisions=true`.
4. Host применяет этот focus к diff и evidence, затем сам формирует Findings, Changes, Manual Test Plan и Open Questions / Could Not Verify.
5. При сложном cross-repository, debugging, security/payment/fraud-sensitive или high-blast-radius случае host может один раз запустить bounded read-only `qa_deep` в своей среде. Решение о запуске и интерпретация результата принадлежат host.
6. После outcome `completed`, `partial` или `blocked` host один раз вызывает `record_qa_task_outcome`.

## Review profiles

Используй наиболее узкий подходящий профиль:

- `pr_test_analyzer` — test intent, branch coverage, missing regression protection;
- `code_reviewer` — changed surface, caller impact, contracts and failure paths;
- `security_reviewer` — auth, authorization, validation, secrets and data exposure;
- `silent_failure_hunter` — swallowed errors, fallback paths, false success and observability;
- `code_explorer` — dependency map, callers, data flow and affected surface;
- `typescript_reviewer` — TypeScript types, async boundaries, serialization and build safety;
- `react_reviewer` — React state, effects, rendering, props and user-visible behavior.

Каждое ревью должно отделять подтверждённые findings от hypotheses и unverified runtime/release evidence. Пустой или неизвестный profile отклоняется до формирования route.

## Metrics contract

`record_qa_task_outcome` принимает только content-free поля:

- `task_type`, `outcome`;
- CodeGraph/source call counters;
- identified/confirmed/rejected findings и repeated source reads;
- optional `deep_analysis_used`, `deep_model`, `deep_reasoning`, duration/token measurements;
- aggregate response-token counters, если они реально измерены.

Не отправляй issue keys, titles, paths, source text, code, logs, prompts, screenshots или generated content. Неизмеренный counter нужно опустить; `0` означает измеренный нулевой результат. `avoided_source_read_tokens` — явно обозначенная оценка, а не доказанный counterfactual.

Metrics JSONL ограничен retention и числом событий. `get_metrics_report(days)` возвращает только агрегаты и data-quality counters.

## MCP tools

Router должен публиковать ровно:

- `prepare_review_route`;
- `record_qa_task_outcome`;
- `get_metrics_report`.

Не добавляй tool, который генерирует текст, принимает evidence, меняет внешний state, выбирает host model или скрыто вызывает другой агент.

## Persistence and safety

Сервис не хранит task content, conversation history, source cache или persistent QA memory. При добавлении поля сначала проверь, что его можно агрегировать без раскрытия источника и что финальное решение по-прежнему принимает host agent.
