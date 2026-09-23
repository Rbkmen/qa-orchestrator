"""Local, read-only installation checks for QA Orchestrator."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import Settings
from .events import validate_metrics_storage
from .model_policy import load_model_selection

MINIMUM_PYTHON = (3, 12)


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    status: str
    detail: str

    @property
    def ok(self) -> bool:
        return self.status != "fail"


def collect_checks() -> list[CheckResult]:
    """Return local checks without creating files or contacting external services."""

    checks: list[CheckResult] = []
    current = sys.version_info[:2]
    python_detail = f"Python {current[0]}.{current[1]} (requires {MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]}+)"
    checks.append(
        CheckResult(
            name="python",
            status="pass" if current >= MINIMUM_PYTHON else "fail",
            detail=python_detail,
        )
    )

    missing = [
        package
        for package in ("fastmcp", "pydantic")
        if importlib.util.find_spec(package) is None
    ]
    checks.append(
        CheckResult(
            name="dependencies",
            status="fail" if missing else "pass",
            detail=(
                f"missing: {', '.join(missing)}"
                if missing
                else "fastmcp and pydantic are available"
            ),
        )
    )

    try:
        settings = Settings.from_env()
    except (TypeError, ValueError) as exc:
        checks.append(CheckResult("configuration", "fail", f"invalid environment value: {exc}"))
    else:
        data_dir = settings.data_dir
        if data_dir.exists():
            detail = f"{data_dir} exists"
        else:
            detail = f"{data_dir} will be created on first metrics write"
        try:
            validate_metrics_storage(settings.metrics_path)
        except OSError as exc:
            metrics_status = "fail"
            detail = str(exc)
        else:
            metrics_status = "pass"
        checks.append(
            CheckResult(
                name="metrics directory",
                status=metrics_status,
                detail=detail,
            )
        )
        try:
            selection = load_model_selection(settings.model_policy_path)
        except ValueError as exc:
            checks.append(CheckResult("model policy", "fail", str(exc)))
        else:
            configured = settings.model_policy_path.exists()
            checks.append(
                CheckResult(
                    name="model policy",
                    status="pass" if configured else "info",
                    detail=(
                        f"{selection.provider.value} policy loaded from {settings.model_policy_path}"
                        if configured
                        else "default OpenAI policy is active; run 'qa-orch setup' to choose Anthropic"
                    ),
                )
            )

    launcher = Path.cwd() / "scripts" / "qa-orchestrator"
    if launcher.exists():
        executable = os.access(launcher, os.X_OK)
        checks.append(
            CheckResult(
                name="source launcher",
                status="pass" if executable else "fail",
                detail=f"{launcher} is {'executable' if executable else 'not executable'}",
            )
        )
    else:
        checks.append(
            CheckResult(
                name="source launcher",
                status="info",
                detail="not in a source checkout; installed entry points are sufficient",
            )
        )

    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check a local QA Orchestrator installation")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    args = parser.parse_args(argv)
    checks = collect_checks()

    if args.json:
        print(json.dumps([asdict(check) for check in checks], ensure_ascii=False))
    else:
        print("QA Orchestrator doctor")
        for check in checks:
            print(f"{check.status.upper():5} {check.name}: {check.detail}")

    return 0 if all(check.ok for check in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
