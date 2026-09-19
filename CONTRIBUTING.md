# Contributing to QA Router MCP

QA Router MCP должен оставаться маленьким детерминированным сервисом: маршрутизация профиля ревью, content-free task metrics и ничего, что подменяет primary agent.

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

Полный тестовый набор не должен требовать сети, credentials или отдельного сервиса.

## Архитектурные границы

- Primary host agent получает sources, строит evidence и принимает финальное QA-решение.
- `prepare_review_route` возвращает только статические metadata выбранного профиля.
- QA Router не создаёт threads/agents, не пишет файлы по запросу пользователя и не выполняет записи во внешние системы.
- `qa_deep` остаётся optional bounded read-only возможностью host client. Router не запускает его сам.
- Metrics содержат только тип задачи, outcome, counters и optional deep measurements; task content запрещён.
- Нельзя добавлять persistent QA memory, learning layer, source cache или скрытые внешние вызовы.

## Изменение MCP-контракта

Публичная поверхность должна оставаться ограниченной тремя инструментами:

1. `prepare_review_route` — profile metadata;
2. `record_qa_task_outcome` — content-free counters;
3. `get_metrics_report` — агрегированный read-only отчёт.

При изменении контракта обнови `contracts.py`, `service.py`, `server.py`, тесты, README, routing policy и все client rules. Для каждой новой ветки добавь проверку входа, ошибочного профиля или несогласованных counters.

## Тестирование

```bash
uv run pytest -q
uv run ruff check .
```

Тесты должны проверять наблюдаемое поведение: точный набор MCP-инструментов, каждый профиль, read-only флаги, недопустимые counters, retention и отсутствие task content в JSONL.

## Документация и клиентские правила

Обновляй [docs/ROUTING_POLICY.md](docs/ROUTING_POLICY.md) при изменении границ ответственности. При изменении поведения host agent обновляй guides в `docs/clients/` и шаблоны в `client-rules/`. Не добавляй в примеры внутренние URL, учетные данные, issue data, локальные абсолютные пути или source payloads.

## Коммиты и review

- Делай сфокусированные изменения.
- Описывай мотивацию, изменение поведения и проверку.
- Отдельно отмечай то, что не удалось проверить.
- Перед review проверь `pytest`, Ruff, `git diff --check` и отсутствие generated/local artifacts.
