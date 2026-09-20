# Codex setup

Codex подключает QA Router как локальный STDIO MCP-сервер. Launcher использует `.venv` проекта, активный `VIRTUAL_ENV` или установленный `qa-router-mcp` из `PATH`.

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

Primary Codex остаётся host и владельцем evidence, решений и внешних действий. Для orchestration используй шесть tools: `start_qa_orchestration`, `advance_qa_orchestration`, `get_qa_orchestration`, `prepare_review_route`, `record_qa_task_outcome` и `get_metrics_report`.

Model policy host-owned: `gpt-5.6-luna/max` делает triage, `gpt-5.6-terra/medium` — primary review и synthesis, а optional `gpt-5.6-sol/high` — read-only deep analysis. Передавай Router только структурированные сигналы; prompts, evidence и model outputs остаются в Codex.

На Luna выбирай fixed bundle или один compatibility profile. Для обычного MR bundle `ordinary_mr` порядок такой: `code_explorer` (`Faraday — Evidence Investigator`) → `code_reviewer` (`Code Reviewer`) → `pr_test_analyzer` (`Test Analyzer`). После каждой Terra-роли передавай её идентификатор в `completed_profile`; synthesis или Sol доступны только после последней роли. Faraday — внутреннее имя профиля, не отдельный внешний сервис или model. Статусы показывай как `Luna / Max` → `Terra / Medium` по ролям → `Terra / Medium` synthesis → `Host` final outcome; передавай исходный `run_id` в финальный `record_qa_task_outcome`.

Reference: [official Codex MCP documentation](https://developers.openai.com/codex/mcp).
