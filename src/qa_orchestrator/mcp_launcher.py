"""Sanitized process entry point for MCP clients."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


def sanitized_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return the small environment the MCP server is allowed to inherit."""

    source = os.environ if source is None else source
    home = source.get("HOME") or source.get("USERPROFILE") or str(Path.home())
    data_dir = source.get("QA_ORCHESTRATOR_DATA_DIR") or str(Path(home) / ".qa-orchestrator")
    path_entries = [str(Path(home) / ".local" / "bin")]

    if os.name == "nt":
        system_root = source.get("SystemRoot") or source.get("WINDIR")
        if system_root:
            path_entries.extend((str(Path(system_root) / "System32"), system_root))
    else:
        path_entries.extend(("/opt/homebrew/bin", "/usr/bin", "/bin"))

    environment = {
        "HOME": home,
        "PATH": os.pathsep.join(path_entries),
        "QA_ORCHESTRATOR_ORCHESTRATION_TTL_SECONDS": source.get(
            "QA_ORCHESTRATOR_ORCHESTRATION_TTL_SECONDS"
        )
        or "1800",
        "QA_ORCHESTRATOR_ORCHESTRATION_MAX_SESSIONS": source.get(
            "QA_ORCHESTRATOR_ORCHESTRATION_MAX_SESSIONS"
        )
        or "100",
        "QA_ORCHESTRATOR_DATA_DIR": data_dir,
        "QA_ORCHESTRATOR_MODEL_POLICY_PATH": source.get(
            "QA_ORCHESTRATOR_MODEL_POLICY_PATH"
        )
        or str(Path(data_dir) / "model-policy.json"),
        "QA_ORCHESTRATOR_SESSION_STORE_PATH": source.get(
            "QA_ORCHESTRATOR_SESSION_STORE_PATH", ""
        ),
    }

    if os.name == "nt":
        environment["USERPROFILE"] = home
        for name in ("SystemRoot", "WINDIR", "HOMEDRIVE", "HOMEPATH", "TEMP", "TMP"):
            value = source.get(name)
            if value:
                environment[name] = value

    return environment


def main() -> None:
    """Start the MCP server after removing unrelated inherited variables."""

    environment = sanitized_environment()
    os.environ.clear()
    os.environ.update(environment)

    from qa_orchestrator.server import main as serve

    serve()
