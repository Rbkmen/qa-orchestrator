# Generic MCP client setup

QA Router использует стандартный MCP STDIO transport. Совместимый клиент должен запускать команду, обмениваться MCP-сообщениями через stdin/stdout и показывать tools host agent.

## Конфигурация

```json
{
  "mcpServers": {
    "qa-router": {
      "command": "/absolute/path/to/qa-router-mcp/scripts/qa-router-mcp",
      "args": [],
      "env": {}
    }
  }
}
```

Точный файл зависит от клиента. Используй абсолютный путь к launcher.

## Инструкции

Скопируй или объедини [`client-rules/generic/QA_ROUTER_INSTRUCTIONS.md`](../../client-rules/generic/QA_ROUTER_INSTRUCTIONS.md) с persistent instructions клиента. Host должен сам получать sources, анализировать evidence и выполнять внешние действия.

## Проверка

1. Убедись, что сервер подключается и показывает ровно шесть tools: `prepare_review_route`, `start_qa_orchestration`, `advance_qa_orchestration`, `get_qa_orchestration`, `record_qa_task_outcome`, `get_metrics_report`.
2. Запусти orchestration: Luna/max выбирает fixed bundle или один profile, Terra/medium выполняет роли по одной в фиксированном порядке — после каждой передавай `completed_profile`; только после последней роли возможны optional Sol/high и Terra/medium synthesis.
3. После каждой стадии передавай Router только structured signal; evidence и outputs храни в host agent.
4. Проверь `read_only=true` и `host_owns_decisions=true`, затем заверши orchestrated-задачу одним вызовом `record_qa_task_outcome` с её `run_id`.
5. Проверь aggregate counters через `get_metrics_report`.

Для обычного MR используй `ordinary_mr`: `code_explorer` (`Faraday — Evidence Investigator`) → `code_reviewer` → `pr_test_analyzer`. Остальные fixed bundles и отображаемые имена перечислены в [routing policy](../ROUTING_POLICY.md); Faraday — только внутреннее имя роли.
