# QA Router MCP

QA Router MCP — небольшой детерминированный FastMCP-сервис для маршрутизации QA-ревью и обезличенных метрик. Он не собирает evidence, не пишет ревью, не принимает QA-решения, не создаёт агентов и не выполняет внешние действия. Всё это остаётся у primary host agent.

## Как работает

1. Primary agent классифицирует задачу и получает evidence из Jira, GitLab, TestRail, Sentry, Grafana, OpenSearch, Slack, Confluence и локального кода.
2. Для implementation-aware ревью он вызывает `prepare_review_route` с одним из семи профилей. Сервис возвращает неизменяемый маршрут: focus, обязательные секции, ограничения и сигналы для эскалации.
3. Primary agent сам анализирует diff, контракты, callers, runtime-доказательства и результаты проверок. Профиль — это чеклист направления, а не автономный агент.
4. Для сложного cross-repository или security-sensitive анализа host может использовать один bounded read-only `qa_deep` в своей собственной конфигурации. QA Router его не выбирает и не запускает.
5. После завершения QA-задачи host один раз вызывает `record_qa_task_outcome`. Сохраняются только счётчики и технические поля без текста evidence.

## MCP-интерфейс

Сервис публикует ровно три инструмента:

| Инструмент | Назначение |
|---|---|
| `prepare_review_route(agent_profile)` | Детерминированный маршрут для профиля ревью |
| `record_qa_task_outcome(...)` | Обезличенная запись результата QA-задачи |
| `get_metrics_report(days)` | Агрегированный отчёт за положительный период |

Профили: `pr_test_analyzer`, `code_reviewer`, `security_reviewer`, `silent_failure_hunter`, `code_explorer`, `typescript_reviewer`, `react_reviewer`.

Маршрут всегда помечен как `read_only=true` и `host_owns_decisions=true`. Общие секции: `Scope`, `Checklist`, `Candidate Coverage Gaps`, `Positive Observations`, `Unverified`.

## Границы ответственности

Primary agent отвечает за:

- получение и проверку authoritative evidence;
- анализ требований, diff, кода, контрактов, логов и runtime-доказательств;
- findings, severity, release/readiness judgment и финальный QA-ответ;
- любые изменения файлов и записи во внешние системы;
- запуск optional `qa_deep` и проверку его результата.

QA Router отвечает только за статический профиль маршрута и content-free task metrics. Он не хранит prompts, source text, логи, draft content или persistent QA memory.

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

Подключи `scripts/qa-router-mcp` как STDIO MCP-сервер. Launcher использует `.venv`, передаёт только каталог метрик и параметры retention и не требует отдельного фонового сервиса.

Пример для Codex:

```bash
codex mcp add qa-router -- \
  /absolute/path/to/qa-router-mcp/scripts/qa-router-mcp
```

Другие варианты подключения описаны в [client guides](docs/clients/).

## Конфигурация

По умолчанию метрики записываются в `$HOME/.qa-router/metrics.jsonl`. Допустимые переменные launcher:

| Переменная | Значение по умолчанию |
|---|---:|
| `QA_ROUTER_DATA_DIR` | `$HOME/.qa-router` |
| `QA_ROUTER_METRICS_RETENTION_DAYS` | `30` |
| `QA_ROUTER_METRICS_MAX_EVENTS` | `10000` |

Все остальные решения о маршрутизации и QA остаются в host agent и не задаются через конфигурацию сервиса.

## Метрики

`record_qa_task_outcome` принимает тип задачи, outcome, количество обращений к CodeGraph и source MCP, counters findings, repeated reads и optional `deep_*` measurements. Значения должны быть неотрицательными и согласованными.

JSONL хранит только aggregate counters: без issue keys, путей, исходного текста, кода, логов, prompts и ответов. Retention и лимит событий применяются при записи. Отчёт можно получить через MCP или локально:

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

См. [CONTRIBUTING.md](CONTRIBUTING.md). Любое изменение публичного MCP-контракта должно сопровождаться тестом точного tool surface и проверкой того, что evidence, решения и внешние записи остаются у host agent.
