# Claude Code setup

Claude Code может запускать QA Router как локальный STDIO MCP-сервер. Используй user scope для всех проектов или local scope для одного проекта.

## Подключение

```bash
claude mcp add --transport stdio --scope user qa-router -- \
  /absolute/path/to/qa-router-mcp/scripts/qa-router-mcp
```

Проверь `claude mcp get qa-router`, `claude mcp list` или `/mcp` внутри Claude Code.

## Инструкции host agent

Для проекта без `CLAUDE.md`:

```bash
cp /absolute/path/to/qa-router-mcp/client-rules/claude-code/CLAUDE.md \
  /absolute/path/to/your-project/CLAUDE.md
```

Если файл уже есть, объедини правила вручную. Claude Code остаётся владельцем evidence, анализа, findings, изменений и external writes. Router возвращает только profile route и metrics receipt.

Reference: [official Claude Code MCP documentation](https://code.claude.com/docs/en/mcp).
