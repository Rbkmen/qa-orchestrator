"""Codex review lifecycle gate; no source code or prompts are persisted."""
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

JIRA = re.compile(r"https://[A-Za-z0-9.-]+/browse/[A-Za-z][A-Za-z0-9]*-\d+", re.IGNORECASE)
MR = re.compile(r"https://[A-Za-z0-9.-]+/[\w.\-/]+/-/merge_requests/\d+", re.IGNORECASE)
REASON = ("QA gate: не удалось подтвердить завершение QA Orchestrator для всех MR. "
          "Сначала определи по запросу, требовалось ли implementation QA-ревью. "
          "Если требовалось, выполни необходимые доступные этапы и укажи реальные "
          "ограничения; если нет или пользователь отменил работу, ответь по запросу.")


def decide(state, event):
    kind = event.get("hook_event_name")
    if kind == "UserPromptSubmit":
        text = event.get("prompt") or ""
        if text != REASON:
            state.clear()
            if re.search(r'(?i)\bне\s+(?:делай|проверяй|проводить)\b', text):
                return {}
            issues = JIRA.findall(text)
            mrs = MR.findall(text)
            if issues and mrs:
                state.update(issues=sorted(set(issues)), expected=sorted(set(mrs)), runs={}, done=[])
    elif kind == "PostToolUse" and state.get("expected"):
        tool = event.get("tool_name", "")
        response = event.get("tool_response")
        args = event.get("tool_input")
        if not isinstance(response, dict) or not isinstance(args, dict):
            return {}
        if response.get("isError"):
            return {}
        content = response.get("structuredContent")
        if not isinstance(content, dict):
            return {}
        if tool.endswith("__start_qa_orchestration") and args.get("task_type") in {"ordinary_review", "widget_review"}:
            context = args.get("review_context") or {}
            manifest = context.get("input_manifest") or {}
            sources = manifest.get("sources") or []
            run = content.get("run_id")
            if isinstance(run, str) and isinstance(sources, list):
                selected = context.get('current_merge_request_source_id')
                mrs = [source.get('locator') for source in sources if isinstance(source, dict) and source.get('kind') == 'merge_request' and source.get('locator') in state['expected'] and (selected is None or selected == source.get('source_id'))]
                issues = [source.get('locator') for source in sources if isinstance(source, dict) and source.get('kind') in {'task', 'jira', 'issue'}]
                if len(mrs) == 1 and set(state.get('issues', [])) & set(issues):
                    state["runs"][run] = mrs[0]
                    if mrs[0] in state['done']:
                        state['done'].remove(mrs[0])
        elif tool.endswith("__finish_qa_orchestration"):
            run = args.get("run_id")
            if run in state["runs"] and content.get("status") in {"completed", "partial", "blocked"}:
                mr = state["runs"][run]
                if mr not in state["done"]:
                    state["done"].append(mr)
    elif kind == "Stop" and state.get('issues') and set(state.get("expected", [])) - set(state.get("done", [])):
        if event.get('stop_hook_active'):
            return {}
        return {"decision": "block", "reason": REASON}
    return {}


def main():
    event = json.load(sys.stdin)
    session = event.get("session_id")
    if not isinstance(session, str) or not session:
        print(json.dumps({"systemMessage": "QA gate: отсутствует session_id, проверка не выполнена."}))
        return
    root = Path(tempfile.gettempdir()) / ("qa-review-gate-" + str(os.getuid()))
    root.mkdir(mode=0o700, exist_ok=True)
    if root.is_symlink() or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise RuntimeError("unsafe state directory")
    path = root / (hashlib.sha256(session.encode()).hexdigest() + ".json")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            state = json.load(stream)
        except json.JSONDecodeError:
            state = {}
        result = decide(state, event)
        stream.seek(0)
        stream.truncate()
        json.dump(state, stream)
        stream.flush()
    print(json.dumps(result))


if __name__ == "__main__":
    main()
