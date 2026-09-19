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

Если файл уже есть, объедини правила вручную. Claude Code остаётся владельцем evidence, анализа, findings, model calls, изменений и external writes. Router публикует шесть инструментов для route, orchestration state и aggregate metrics; он возвращает только структурированные content-free данные.

Используй flow `gpt-5.6-luna/max` → `gpt-5.6-terra/medium` для выбранных ролей → optional `gpt-5.6-sol/high` → `gpt-5.6-terra/medium` synthesis. Luna выбирает fixed bundle или один compatibility profile; порядок bundle менять нельзя. Для `ordinary_mr`: `Faraday — Evidence Investigator` → `Code Reviewer` → `Test Analyzer`. После каждой стадии вызывай `advance_qa_orchestration`, а итог записывай одним вызовом `record_qa_task_outcome`.

Reference: [official Claude Code MCP documentation](https://code.claude.com/docs/en/mcp).
