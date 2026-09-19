# QA Router Policy

Эта policy client-neutral. **Primary host** — Codex, Claude Code, Cursor или другой MCP-клиент — остаётся главным оркестратором и владельцем решений.

## Responsibility boundary

Primary host owns:

- task classification и получение authoritative sources;
- requirements, diff, code, contract, log и runtime analysis;
- запуск model stages по фиксированной policy и валидацию ответов;
- findings, severity, coverage, release/readiness judgment и финальный ответ;
- CodeGraph и source-MCP calls;
- code/file changes и все записи во внешние системы.

QA Router owns only deterministic profile routing, content-free orchestration state and aggregate metrics. Router не принимает evidence, prompts или model outputs и не выполняет autonomous writes.

## Model policy

| Стадия | Model | Reasoning | Ответственность |
|---|---|---|---|
| Triage | `gpt-5.6-luna` | `max` | Выбор fixed review bundle или compatibility profile и выявление evidence gaps |
| Primary review | `gpt-5.6-terra` | `medium` | Последовательное implementation-aware ревью выбранных профилей |
| Deep escalation | `gpt-5.6-sol` | `high` | Опциональная read-only проверка сложного или рискованного случая |
| Synthesis | `gpt-5.6-terra` | `medium` | Сведение результата после проверки host |

Router только возвращает следующую policy и transition constraints. Primary host запускает модели в своей среде, проверяет findings и сам принимает финальное решение.

## Orchestration flow

1. Host вызывает `start_qa_orchestration(task_type)` и получает `run_id`, Luna/max и next action.
2. После triage host передаёт `advance_qa_orchestration` с одним fixed bundle или одним из семи `ReviewAgent` profiles.
3. Для bundle host запускает Terra по одному профилю в возвращённом порядке и после каждой роли передаёт её идентификатор в `completed_profile`; Router не пропускает роль и не принимает произвольный порядок.
4. После последнего Terra profile host либо идёт напрямую в Terra synthesis, либо передаёт fixed `reason_code` и получает optional Sol/high.
5. После Sol host возвращается к Terra synthesis.
6. После synthesis состояние становится `awaiting_host_outcome`; host один раз вызывает `record_qa_task_outcome` с тем же `run_id` и статусом `completed`, `partial` или `blocked`. Router переводит сессию в финальный статус.

Разрешённые переходы:

```text
Luna triage → Terra profile[1] → ... → Terra profile[N]
                                      ↘ Sol deep review ↗
                                         Terra synthesis → awaiting host outcome
```

Сессии content-free, in-memory, с TTL `1800` секунд и лимитом `100` активных сессий по умолчанию; общий cache bounded, поэтому старые terminal-сессии могут быть вытеснены при создании новых. Unknown run, expired session, illegal/repeated transition и invalid signal отклоняются без изменения состояния. После рестарта host начинает новую сессию.

Обычный status flow для `ordinary_mr`:

```text
Luna / Max → Ordinary MR Review
Terra / Medium → Faraday — Evidence Investigator
Terra / Medium → Code Reviewer
Terra / Medium → Test Analyzer
Terra / Medium → Synthesis
Host → Final QA outcome
```

`Sol / High → Deep read-only review` появляется только после последнего Terra profile и при одном fixed `reason_code`, затем возвращает поток к Terra synthesis.

## Fixed bundles и имена профилей

| Bundle | Ordered profiles |
|---|---|
| `ordinary_mr` | `code_explorer` → `code_reviewer` → `pr_test_analyzer` |
| `widget` | `code_explorer` → `react_reviewer` → `typescript_reviewer` → `pr_test_analyzer` |
| `security` | `code_explorer` → `security_reviewer` → `silent_failure_hunter` |
| `autotest` | `code_reviewer` → `pr_test_analyzer` → `typescript_reviewer` |
| `requirements` | `code_explorer` → `code_reviewer` |

| Technical profile | Display name |
|---|---|
| `code_explorer` | `Faraday — Evidence Investigator` |
| `code_reviewer` | `Code Reviewer` |
| `pr_test_analyzer` | `Test Analyzer` |
| `security_reviewer` | `Security Reviewer` |
| `silent_failure_hunter` | `Silent Failure Hunter` |
| `typescript_reviewer` | `TypeScript Reviewer` |
| `react_reviewer` | `React Reviewer` |

Faraday — внутреннее отображаемое имя `code_explorer`. Это не отдельный внешний агент, сервис, package или model. Router возвращает только фиксированный идентификатор и порядок, а host запускает Terra для каждой роли.

## Review profiles

Используй наиболее узкий подходящий профиль:

- `pr_test_analyzer` — test intent, branch coverage, missing regression protection;
- `code_reviewer` — changed surface, caller impact, contracts and failure paths;
- `security_reviewer` — auth, authorization, validation, secrets and data exposure;
- `silent_failure_hunter` — swallowed errors, fallback paths, false success and observability;
- `code_explorer` — dependency map, callers, data flow and affected surface;
- `typescript_reviewer` — TypeScript types, async boundaries, serialization and build safety;
- `react_reviewer` — React state, effects, rendering, props and user-visible behavior.

Каждое ревью должно отделять confirmed findings от hypotheses и unverified runtime/release facts. Пустой или неизвестный profile отклоняется до формирования route.

## Metrics contract

`record_qa_task_outcome` принимает только content-free поля:

- `task_type`, `outcome`;
- CodeGraph/source call counters;
- identified/confirmed/rejected findings и repeated source reads;
- optional `deep_*` measurements;
- `orchestration_used`, `luna_calls`, `terra_calls`, `sol_calls`, `orchestration_steps_completed`, `orchestration_retries`.

При `orchestration_used=true` поле `run_id` обязательно. Оно используется только для связывания финального outcome и aggregate counters с in-memory сессией, проверяется относительно выбранной ветки и не сохраняется в JSONL.

Orchestration counters неотрицательны и не принимаются как положительные, если orchestration не использовался. Не отправляй issue keys, titles, paths, source text, code, logs, screenshots или generated content. `get_metrics_report(days)` возвращает только агрегаты и data-quality counters.

## MCP tools

Router должен публиковать ровно:

- `prepare_review_route`;
- `start_qa_orchestration`;
- `advance_qa_orchestration`;
- `get_qa_orchestration`;
- `record_qa_task_outcome`;
- `get_metrics_report`.

`read_only=true` и `host_owns_decisions=true` должны сохраняться во всех orchestration states. Не добавляй tool, который генерирует текст, принимает evidence, меняет внешний state, выбирает модель за host или скрыто вызывает другой агент.

## Persistence and safety

Сервис не хранит task content, conversation history, source cache или persistent QA memory. При добавлении поля сначала проверь, что его можно агрегировать без раскрытия источника и что финальное решение по-прежнему принимает primary host.
