# Cursor setup

Cursor подключает QA Router через `mcp.json`. Используй `$HOME/.cursor/mcp.json` для всех проектов или `.cursor/mcp.json` для одного проекта.

## Подключение

Создай или объедини конфигурацию, не затирая существующие серверы:

```json
{
  "mcpServers": {
    "qa-router": {
      "command": "/absolute/path/to/qa-router-mcp/scripts/qa-router-mcp",
      "args": []
    }
  }
}
```

Перезапусти Cursor и проверь, что доступны ровно шесть инструментов QA Router.

## Инструкции host agent

```bash
mkdir -p /absolute/path/to/your-project/.cursor/rules
cp /absolute/path/to/qa-router-mcp/client-rules/cursor/qa-router.mdc \
  /absolute/path/to/your-project/.cursor/rules/qa-router.mdc
```

Правило заставляет Cursor Agent получать evidence, выполнять model stages и принимать QA-решения самостоятельно. Используй `start_qa_orchestration` → Luna выбирает fixed bundle или profile → `advance_qa_orchestration` → `get_qa_orchestration`, а `prepare_review_route` — для каждой роли в фиксированном порядке, а не как отдельный внешний агент. Для `ordinary_mr` порядок: `Faraday — Evidence Investigator` → `Code Reviewer` → `Test Analyzer`; после каждой роли передавай её `completed_profile`, а Sol/synthesis запускай только после последней. Policy: Luna/max для triage, Terra/medium для primary/synthesis и optional Sol/high для read-only escalation. Финальный `record_qa_task_outcome` получает исходный `run_id`.

References: [official Cursor MCP documentation](https://docs.cursor.com/context/model-context-protocol) и [Cursor Rules documentation](https://cursor.com/docs/rules).
