# Codex setup

Codex подключает QA Router как локальный STDIO MCP-сервер. Команда выполняется из `.venv` проекта через launcher.

## Подключение

Из каталога репозитория получи абсолютный путь:

```bash
pwd
```

Добавь сервер:

```bash
codex mcp add qa-router -- \
  /absolute/path/to/qa-router-mcp/scripts/qa-router-mcp
```

Или добавь в `$HOME/.codex/config.toml`:

```toml
[mcp_servers.qa-router]
command = "/absolute/path/to/qa-router-mcp/scripts/qa-router-mcp"
args = []
startup_timeout_sec = 30
tool_timeout_sec = 120
```

Проверь регистрацию командой `codex mcp list` и перезапусти Codex.

## Инструкции host agent

Добавь правила из [`client-rules/generic/QA_ROUTER_INSTRUCTIONS.md`](../../client-rules/generic/QA_ROUTER_INSTRUCTIONS.md) в persistent project instructions или адаптируй их под свои Codex rules. Не устанавливай отдельный routing skill: достаточно MCP-сервера и этих инструкций.

Primary Codex собирает evidence, вызывает `prepare_review_route`, выполняет ревью сам и после завершения записывает один content-free outcome. `qa_deep`, если он нужен, остаётся отдельной host-owned read-only эскалацией.

Reference: [official Codex MCP documentation](https://developers.openai.com/codex/mcp).
