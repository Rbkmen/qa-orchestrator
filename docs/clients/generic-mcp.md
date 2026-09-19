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

1. Убедись, что сервер подключается и показывает `prepare_review_route`, `record_qa_task_outcome`, `get_metrics_report`.
2. Вызови `prepare_review_route` для каждого нужного профиля и проверь read-only флаги.
3. Выполни маленькое read-only QA-ревью в host agent.
4. Заверши его одним вызовом `record_qa_task_outcome`.
5. Проверь агрегаты через `get_metrics_report`.
