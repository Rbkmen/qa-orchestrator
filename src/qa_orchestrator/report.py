import argparse
import json
import sys
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field

from qa_orchestrator.config import Settings
from qa_orchestrator.events import (
    JsonEventSink,
    normalize_task_distribution_event,
    read_metrics_lines,
)


class TaskDistributionReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period_days: int = Field(ge=1)
    total_tasks: int
    by_task_type: dict[str, int]


def summarize_events(lines: Iterable[str], days: int = 7) -> dict[str, object]:
    cutoff = datetime.now(UTC) - timedelta(days=days)
    task_types: Counter[str] = Counter()

    for line in lines:
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        distribution_event = normalize_task_distribution_event(event)
        if distribution_event is None:
            continue
        timestamp = _event_timestamp(distribution_event)
        if timestamp is not None and timestamp >= cutoff:
            task_types[str(distribution_event["task_type"])] += 1

    return {
        "period_days": days,
        "total_tasks": sum(task_types.values()),
        "by_task_type": dict(sorted(task_types.items())),
    }


def _event_timestamp(event: dict[str, object]) -> datetime | None:
    try:
        timestamp = datetime.fromisoformat(str(event["timestamp"]))
    except (KeyError, TypeError, ValueError):
        return None
    return timestamp if timestamp.tzinfo is not None else timestamp.replace(tzinfo=UTC)


def main() -> None:
    parser = argparse.ArgumentParser(description="Show QA task distribution")
    parser.add_argument("--days", type=int, default=7, help="report window in days")
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days must be positive")
    settings = Settings.from_env()
    events = JsonEventSink(
        settings.metrics_path,
        settings.metrics_retention_days,
        settings.metrics_max_events,
    )
    if not events.sanitize_existing_records():
        print(
            "Existing task-distribution data could not be sanitized; "
            "reporting remains aggregate-only.",
            file=sys.stderr,
        )
    report = summarize_events(read_metrics_lines(settings.metrics_path), args.days)
    print(json.dumps(report, indent=2))
