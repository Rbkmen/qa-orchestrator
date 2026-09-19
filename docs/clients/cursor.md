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

Правило заставляет Cursor Agent получать evidence, выполнять model stages и принимать QA-решения самостоятельно. Используй `start_qa_orchestration` → `advance_qa_orchestration` → `get_qa_orchestration`, а `prepare_review_route` — как детерминированный профиль, а не как отдельный агент. Policy: Luna/max для triage, Terra/medium для primary/synthesis и optional Sol/high для read-only escalation.

References: [official Cursor MCP documentation](https://docs.cursor.com/context/model-context-protocol) и [Cursor Rules documentation](https://cursor.com/docs/rules).
