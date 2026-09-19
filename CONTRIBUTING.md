# Contributing to QA Router MCP

QA Router MCP должен оставаться маленьким детерминированным сервисом: fixed routing, host-owned orchestration и content-free metrics.

## Разработка

Требования:

- Python 3.12+;
- [`uv`](https://docs.astral.sh/uv/);
- MCP-клиент для ручной проверки STDIO.

```bash
git clone https://github.com/Rbkmen/qa-router-mcp.git
cd qa-router-mcp
uv sync
uv run pytest -q
uv run ruff check .
```

Полный тестовый набор не должен требовать сети, credentials или отдельного внешнего сервиса.

## Архитектурные границы

- Primary host получает sources, строит evidence, запускает model stages и принимает финальное QA-решение.
- `prepare_review_route` возвращает только статические metadata выбранного профиля.
- `start_qa_orchestration`, `advance_qa_orchestration` и `get_qa_orchestration` управляют только content-free состоянием.
- Router не вызывает модели, не создаёт threads/agents, не пишет файлы по запросу пользователя и не выполняет записи во внешние системы.
- Модельная policy фиксирована: Luna/max для triage, Terra/medium для primary review и synthesis, optional Sol/high для read-only deep analysis.
- Metrics содержат только тип задачи, outcome и агрегированные counters; task content запрещён.
- Нельзя добавлять persistent QA memory, source cache, learning layer или скрытые внешние вызовы.

## Изменение MCP-контракта

Публичная поверхность должна оставаться ограниченной шестью инструментами:

1. `prepare_review_route` — profile metadata;
2. `start_qa_orchestration` — создание сессии;
3. `advance_qa_orchestration` — проверенный переход;
4. `get_qa_orchestration` — состояние и next action;
5. `record_qa_task_outcome` — content-free counters;
6. `get_metrics_report` — агрегированный read-only отчёт.

При изменении контракта обнови `contracts.py`, `orchestration.py`, `service.py`, `server.py`, тесты, README, routing policy и client rules. Для каждой новой ветки добавь проверки входа, illegal transition, expiry/limit и отсутствия task content.

## Тестирование

```bash
uv run pytest -q
uv run ruff check .
git diff --check
```

Тесты должны проверять наблюдаемое поведение: точный набор MCP-инструментов, каждый профиль, model policy, state transitions, read-only флаги, недопустимые counters, retention и отсутствие task content в JSONL.

## Документация и клиентские правила

Обновляй [docs/ROUTING_POLICY.md](docs/ROUTING_POLICY.md) при изменении границ ответственности. При изменении поведения host обновляй guides в `docs/clients/` и шаблоны в `client-rules/`. Не добавляй в примеры внутренние URL, credentials, issue data, локальные абсолютные пути или source payloads.

## Коммиты и review

- Делай сфокусированные изменения.
- Описывай мотивацию, изменение поведения и проверку.
- Отдельно отмечай то, что не удалось проверить.
- Перед review проверь `pytest`, Ruff, `git diff --check`, точный tool surface и отсутствие generated/local artifacts.
