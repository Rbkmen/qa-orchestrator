import argparse
import json
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from os import environ
from pathlib import Path

from qa_router_mcp.events import (
    ORCHESTRATION_COUNTERS,
    QA_TASK_TOKEN_COUNTERS,
    read_metrics_lines,
    valid_qa_task_metrics,
)


def summarize_events(lines: Iterable[str], days: int = 7) -> dict[str, object]:
    cutoff = datetime.now(UTC) - timedelta(days=days)
    outcomes: Counter[str] = Counter()
    task_types: Counter[str] = Counter()
    deep_models: Counter[str] = Counter()
    deep_reasoning: Counter[str] = Counter()
    totals = Counter()

    for line in lines:
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        timestamp = _event_timestamp(event)
        if (
            event.get("event_type") != "qa_task_outcome"
            or event.get("schema_version") != 1
            or timestamp is None
            or timestamp < cutoff
            or not valid_qa_task_metrics(event)
        ):
            continue

        outcomes[str(event["outcome"])] += 1
        task_types[str(event["task_type"])] += 1
        totals["events"] += 1
        totals["deep_tasks"] += event["deep_analysis_used"] is True
        totals["codegraph_tasks"] += event["codegraph_calls"] > 0
        totals["complete_token_measurement_tasks"] += QA_TASK_TOKEN_COUNTERS <= event.keys()
        if event["deep_analysis_used"] is True:
            deep_models[str(event.get("deep_model", "unknown"))] += 1
            deep_reasoning[str(event.get("deep_reasoning", "unknown"))] += 1
        if event.get("orchestration_used") is True:
            totals["orchestration_tasks"] += 1
        for field in (
            "codegraph_calls",
            "source_mcp_calls",
            "findings_identified",
            "findings_confirmed",
            "findings_rejected",
            "repeated_source_reads",
            "codegraph_response_tokens",
            "source_mcp_response_tokens",
            "avoided_source_read_tokens",
            "deep_duration_ms",
            "deep_input_tokens",
            "deep_output_tokens",
            *ORCHESTRATION_COUNTERS,
        ):
            value = event.get(field, 0)
            if type(value) is int and value >= 0:
                totals[field] += value

    return {
        "period_days": days,
        "qa_tasks": {
            "events": totals["events"],
            "outcomes": dict(sorted(outcomes.items())),
            "by_task_type": dict(sorted(task_types.items())),
            "codegraph_calls": totals["codegraph_calls"],
            "source_mcp_calls": totals["source_mcp_calls"],
            "deep_tasks": totals["deep_tasks"],
            "deep_by_model": dict(sorted(deep_models.items())),
            "deep_by_reasoning": dict(sorted(deep_reasoning.items())),
            "deep_duration_ms": totals["deep_duration_ms"],
            "deep_input_tokens": totals["deep_input_tokens"],
            "deep_output_tokens": totals["deep_output_tokens"],
            "findings_identified": totals["findings_identified"],
            "findings_confirmed": totals["findings_confirmed"],
            "findings_rejected": totals["findings_rejected"],
            "repeated_source_reads": totals["repeated_source_reads"],
            "codegraph": {
                "tasks": totals["codegraph_tasks"],
                "calls": totals["codegraph_calls"],
                "response_tokens": totals["codegraph_response_tokens"],
                "avoided_source_read_tokens": totals["avoided_source_read_tokens"],
                "estimated_source_token_savings_pct": _savings_percent(
                    totals["avoided_source_read_tokens"],
                    totals["source_mcp_response_tokens"],
                ),
            },
            "source_mcp_response_tokens": totals["source_mcp_response_tokens"],
            "complete_token_measurement_tasks": totals["complete_token_measurement_tasks"],
            "orchestration": {
                "tasks": totals["orchestration_tasks"],
                "luna_calls": totals["luna_calls"],
                "terra_calls": totals["terra_calls"],
                "sol_calls": totals["sol_calls"],
                "steps_completed": totals["orchestration_steps_completed"],
                "retries": totals["orchestration_retries"],
            },
        },
        "data_quality": {
            "task_events": totals["events"],
            "complete_token_measurement_rate": _coverage_rate(
                totals["complete_token_measurement_tasks"], totals["events"]
            ),
        },
    }


def _event_timestamp(event: dict[str, object]) -> datetime | None:
    try:
        timestamp = datetime.fromisoformat(str(event["timestamp"]))
    except (KeyError, TypeError, ValueError):
        return None
    return timestamp if timestamp.tzinfo is not None else timestamp.replace(tzinfo=UTC)


def _savings_percent(avoided_tokens: int, source_tokens: int) -> float | None:
    total = avoided_tokens + source_tokens
    return round(avoided_tokens / total * 100, 1) if total else None


def _coverage_rate(complete: int, total: int) -> float | None:
    return round(complete / total, 3) if total else None


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize QA task metrics")
    parser.add_argument("--days", type=int, default=7, help="report window in days")
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days must be positive")
    data_dir = Path(environ.get("QA_ROUTER_DATA_DIR", str(Path.home() / ".qa-router")))
    print(json.dumps(summarize_events(read_metrics_lines(data_dir / "metrics.jsonl"), args.days), indent=2))
