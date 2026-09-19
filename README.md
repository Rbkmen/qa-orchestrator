# QA Router MCP

QA Router MCP — небольшой детерминированный FastMCP-сервис для host-owned QA-ревью и обезличенных метрик. Router хранит только ограниченное состояние маршрута: evidence, исходный код, логи, prompts, ответы моделей и финальные решения остаются у primary host agent.

## Как работает

1. Primary host получает authoritative evidence из нужных систем и определяет тип QA-задачи.
2. Host вызывает `start_qa_orchestration`. Router создаёт content-free сессию и возвращает первый шаг — Luna с `max` reasoning.
3. Host выполняет стадии в своей model configuration и после каждой стадии передаёт Router только структурированный сигнал:
   - `gpt-5.6-luna` + `max` — triage и выбор одного fixed review bundle или одного compatibility profile;
   - `gpt-5.6-terra` + `medium` — primary review каждого профиля в фиксированном порядке;
   - опционально `gpt-5.6-sol` + `high` — read-only deep analysis по фиксированной причине;
   - `gpt-5.6-terra` + `medium` — synthesis.
4. Host сам проверяет findings, runtime-доказательства и ограничения, затем один раз вызывает `record_qa_task_outcome`; для orchestrated-задачи передаёт тот же `run_id`, чтобы Router закрыл сессию.

Router не вызывает модели, не выбирает severity или release readiness и не выполняет внешние записи.

### Bundles и имена профилей

Luna выбирает один из фиксированных bundles либо один профиль для обратной совместимости. Terra выполняет профили bundle последовательно; после каждой роли host передаёт `completed_profile`, а Router возвращает `current_profile` и `completed_profiles`. Перейти к Sol или synthesis можно только после последней роли.

| Bundle | Порядок профилей |
|---|---|
| `ordinary_mr` | `code_explorer` → `code_reviewer` → `pr_test_analyzer` |
| `widget` | `code_explorer` → `react_reviewer` → `typescript_reviewer` → `pr_test_analyzer` |
| `security` | `code_explorer` → `security_reviewer` → `silent_failure_hunter` |
| `autotest` | `code_reviewer` → `pr_test_analyzer` → `typescript_reviewer` |
| `requirements` | `code_explorer` → `code_reviewer` |

Технические профили и отображаемые имена:

| Профиль | Имя для host |
|---|---|
| `code_explorer` | `Faraday — Evidence Investigator` |
| `code_reviewer` | `Code Reviewer` |
| `pr_test_analyzer` | `Test Analyzer` |
| `security_reviewer` | `Security Reviewer` |
| `silent_failure_hunter` | `Silent Failure Hunter` |
| `typescript_reviewer` | `TypeScript Reviewer` |
| `react_reviewer` | `React Reviewer` |

Faraday — это только внутреннее отображаемое имя профиля `code_explorer`: отдельный внешний агент, сервис, package или model не подключается.

Обычный status flow:

```text
Luna / Max → Ordinary MR Review
Terra / Medium → Faraday — Evidence Investigator
Terra / Medium → Code Reviewer
Terra / Medium → Test Analyzer
Terra / Medium → Synthesis
Host → Final QA outcome
```

При зафиксированной причине после последней роли добавляется `Sol / High → Deep read-only review` между primary review и synthesis.

## MCP-интерфейс

Сервис публикует ровно шесть инструментов:

| Инструмент | Назначение |
|---|---|
| `prepare_review_route(agent_profile)` | Детерминированный checklist для одного из семи профилей |
| `start_qa_orchestration(task_type)` | Создание host-owned orchestration-сессии |
| `advance_qa_orchestration(...)` | Один структурированный переход между стадиями |
| `get_qa_orchestration(run_id)` | Чтение текущего content-free состояния |
| `record_qa_task_outcome(...)` | Одна обезличенная запись результата QA-задачи |
| `get_metrics_report(days)` | Агрегированный отчёт за положительный период |

Оркестрационный flow для bundle:

```text
Luna/max → Terra/profile[1] → ... → Terra/profile[N]
                                      ↘ optional Sol/high ↗
                                           Terra synthesis → host outcome
```

Сессии хранятся только в памяти процесса. По умолчанию TTL — 1800 секунд, максимум — 100 активных сессий; общий cache также bounded, а старые terminal-сессии могут быть вытеснены при нехватке места. Повтор финального вызова идемпотентен, пока его сессия сохранена. После перезапуска host начинает новую сессию. `read_only=true` и `host_owns_decisions=true` являются частью каждого состояния.

После synthesis сессия ждёт финальный host outcome. Вызов `record_qa_task_outcome` с `orchestration_used=true` обязан содержать `run_id` текущей сессии; Router связывает counters с фактической веткой и переводит её в `completed`, `partial` или `blocked`. Для остановленной на стадии сессии сначала передай в `advance_qa_orchestration` статус `partial` или `blocked`. Повтор того же вызова для того же `run_id` и outcome идемпотентен; для обычной задачи без orchestration `run_id` не передаётся.

### Профили ревью

`prepare_review_route` возвращает focus, обязательные секции, ограничения, escalation signals и отображаемое имя одного профиля. Для bundle host вызывает route для каждого профиля в полученном фиксированном порядке и после каждого вызова передаёт его технический идентификатор в `completed_profile`.

Общие секции route: `Scope`, `Checklist`, `Candidate Coverage Gaps`, `Positive Observations`, `Unverified`.

## Границы ответственности

Primary host отвечает за:

- получение и проверку evidence;
- вызовы Jira, GitLab, TestRail, Sentry, Grafana, OpenSearch, Slack, Confluence, CodeGraph и файловой системы;
- запуск трёх model stages по policy и проверку их результатов;
- confirmed findings, severity, release/readiness judgment и финальный QA-ответ;
- изменения файлов и любые внешние записи.

QA Router отвечает только за fixed routing, state transitions, read-only constraints и content-free metrics. В `advance_qa_orchestration` нельзя передавать Evidence Packet, prompt, model output, source text, logs, paths или произвольную причину.

## Требования

- Python 3.12+;
- [`uv`](https://docs.astral.sh/uv/);
- MCP-клиент, поддерживающий STDIO.

## Установка

```bash
git clone https://github.com/Rbkmen/qa-router-mcp.git
cd qa-router-mcp
uv sync
uv run pytest -q
uv run ruff check .
```

Подключи `scripts/qa-router-mcp` как STDIO MCP-сервер. Launcher передаёт каталог метрик и параметры retention/orchestration, не требует отдельного фонового процесса.

Пример для Codex:

```bash
codex mcp add qa-router -- \
  /absolute/path/to/qa-router-mcp/scripts/qa-router-mcp
```

Другие варианты подключения описаны в [client guides](docs/clients/).

## Конфигурация

По умолчанию метрики записываются в `$HOME/.qa-router/metrics.jsonl`.

| Переменная | Значение по умолчанию |
|---|---:|
| `QA_ROUTER_DATA_DIR` | `$HOME/.qa-router` |
| `QA_ROUTER_METRICS_RETENTION_DAYS` | `30` |
| `QA_ROUTER_METRICS_MAX_EVENTS` | `10000` |
| `QA_ROUTER_ORCHESTRATION_TTL_SECONDS` | `1800` |
| `QA_ROUTER_ORCHESTRATION_MAX_SESSIONS` | `100` |

## Метрики

`record_qa_task_outcome` принимает task type, outcome, counters CodeGraph/source MCP, findings, repeated reads, optional deep-analysis measurements и content-free orchestration counters:

- `orchestration_used`;
- `luna_calls`, `terra_calls`, `sol_calls`;
- `orchestration_steps_completed`, `orchestration_retries`.

Для orchestrated-задачи используй `run_id` из `start_qa_orchestration`; сам opaque идентификатор не записывается в JSONL-метрику.

Значения неотрицательные и согласованные. JSONL не содержит issue keys, путей, исходного текста, кода, логов, prompts или ответов моделей. Отчёт можно получить через MCP или локально:

```bash
uv run qa-router-report
uv run qa-router-report --days 30
```

## Клиенты и правила

- [Codex](docs/clients/codex.md)
- [Claude Code](docs/clients/claude-code.md)
- [Cursor](docs/clients/cursor.md)
- [Generic MCP client](docs/clients/generic-mcp.md)
- [Общая routing policy](docs/ROUTING_POLICY.md)
- [Шаблоны client rules](client-rules/)

## Разработка

См. [CONTRIBUTING.md](CONTRIBUTING.md). Любое изменение публичного MCP-контракта должно сопровождаться тестом точного tool surface и проверкой, что evidence, решения и внешние записи остаются у primary host.
