import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from fcntl import LOCK_EX, LOCK_SH, LOCK_UN, flock
from pathlib import Path
from re import fullmatch
from typing import IO, Protocol

from qa_router_mcp.contracts import QaTaskOutcomeReceipt

QA_TASK_TYPES = {
    "ordinary_review",
    "widget_review",
    "epic_analysis",
    "requirements_analysis",
    "qa_planning",
    "autotest_implementation",
    "other",
}
QA_TASK_OUTCOMES = {"completed", "partial", "blocked"}
QA_TASK_COUNTERS = {
    "codegraph_calls",
    "source_mcp_calls",
    "findings_identified",
    "findings_confirmed",
    "findings_rejected",
    "repeated_source_reads",
}
QA_TASK_TOKEN_COUNTERS = {
    "codegraph_response_tokens",
    "source_mcp_response_tokens",
    "avoided_source_read_tokens",
}
ORCHESTRATION_COUNTERS = {
    "luna_calls",
    "terra_calls",
    "sol_calls",
    "orchestration_steps_completed",
    "orchestration_retries",
}
DEEP_REASONING = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
EVENT_FIELDS = {
    "schema_version",
    "event_type",
    "timestamp",
    "task_type",
    "outcome",
    "deep_analysis_used",
    "deep_model",
    "deep_reasoning",
    "deep_duration_ms",
    "deep_input_tokens",
    "deep_output_tokens",
    "orchestration_used",
    *QA_TASK_COUNTERS,
    *QA_TASK_TOKEN_COUNTERS,
    *ORCHESTRATION_COUNTERS,
}


class EventSink(Protocol):
    def record_qa_task_outcome(self, event: dict[str, object]) -> QaTaskOutcomeReceipt: ...


class JsonEventSink:
    def __init__(
        self,
        path: Path | None = None,
        retention_days: int = 30,
        max_events: int = 10_000,
    ) -> None:
        self.path = path
        self.retention_days = retention_days
        self.max_events = max_events

    def record_qa_task_outcome(self, event: dict[str, object]) -> QaTaskOutcomeReceipt:
        if self.path is None:
            return QaTaskOutcomeReceipt(status="unavailable")
        fields = {
            "task_type",
            "outcome",
            "deep_analysis_used",
            "deep_model",
            "deep_reasoning",
            "deep_duration_ms",
            "deep_input_tokens",
            "deep_output_tokens",
            "orchestration_used",
            *QA_TASK_COUNTERS,
            *QA_TASK_TOKEN_COUNTERS,
            *ORCHESTRATION_COUNTERS,
        }
        payload = {key: event[key] for key in fields if key in event}
        payload.update(
            {
                "schema_version": 1,
                "event_type": "qa_task_outcome",
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )
        return QaTaskOutcomeReceipt(status="recorded" if self._write(payload) else "unavailable")

    @contextmanager
    def _locked_events(self) -> Iterator[tuple[IO[str], list[dict[str, object]]]]:
        if self.path is None:
            raise OSError("metrics path is unavailable")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        with self.path.open("a+", encoding="utf-8") as metrics:
            flock(metrics.fileno(), LOCK_EX)
            try:
                metrics.seek(0)
                events = _parse_events(metrics)
                yield metrics, events
                self.path.chmod(0o600)
            finally:
                flock(metrics.fileno(), LOCK_UN)

    def _write(self, event: dict[str, object]) -> bool:
        line = _serialize(event)
        print(line, file=sys.stderr, flush=True)
        try:
            with self._locked_events() as (metrics, events):
                cutoff = datetime.now(UTC) - timedelta(days=self.retention_days)
                retained = [
                    item
                    for item in [*events, event]
                    if (timestamp := _event_timestamp(item)) is not None and timestamp >= cutoff
                ]
                metrics.seek(0)
                metrics.truncate()
                metrics.write(
                    "".join(_serialize(item) + "\n" for item in retained[-self.max_events :])
                )
                metrics.flush()
            return True
        except OSError:
            return False


def read_metrics_lines(path: Path) -> list[str]:
    try:
        with path.open(encoding="utf-8") as metrics:
            flock(metrics.fileno(), LOCK_SH)
            try:
                return metrics.read().splitlines()
            finally:
                flock(metrics.fileno(), LOCK_UN)
    except FileNotFoundError:
        return []


def valid_qa_task_metrics(event: dict[str, object]) -> bool:
    if event.get("task_type") not in QA_TASK_TYPES or event.get("outcome") not in QA_TASK_OUTCOMES:
        return False
    if set(event) - EVENT_FIELDS:
        return False
    deep_used = event.get("deep_analysis_used")
    if type(deep_used) is not bool:
        return False
    orchestration_used = event.get("orchestration_used", False)
    if type(orchestration_used) is not bool:
        return False
    if "deep_model" in event and (
        not deep_used
        or not isinstance(event["deep_model"], str)
        or not fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,127}", event["deep_model"])
    ):
        return False
    if "deep_reasoning" in event and (
        not deep_used
        or not isinstance(event["deep_reasoning"], str)
        or event["deep_reasoning"] not in DEEP_REASONING
    ):
        return False
    if any(
        field in event and (type(event[field]) is not int or event[field] < 0)
        for field in ("deep_duration_ms", "deep_input_tokens", "deep_output_tokens")
    ):
        return False
    if not deep_used and any(
        event.get(field, 0) > 0
        for field in ("deep_duration_ms", "deep_input_tokens", "deep_output_tokens")
    ):
        return False
    if any(type(event.get(field)) is not int or event[field] < 0 for field in QA_TASK_COUNTERS):
        return False
    if any(
        field in event and (type(event[field]) is not int or event[field] < 0)
        for field in QA_TASK_TOKEN_COUNTERS
    ):
        return False
    if any(
        field in event and (type(event[field]) is not int or event[field] < 0)
        for field in ORCHESTRATION_COUNTERS
    ):
        return False
    if not orchestration_used and any(
        event.get(field, 0) > 0 for field in ORCHESTRATION_COUNTERS
    ):
        return False
    if event["codegraph_calls"] == 0 and any(
        event.get(field, 0) > 0
        for field in ("codegraph_response_tokens", "avoided_source_read_tokens")
    ):
        return False
    if event["source_mcp_calls"] == 0 and event.get("source_mcp_response_tokens", 0) > 0:
        return False
    if event["repeated_source_reads"] > event["source_mcp_calls"]:
        return False
    return event["findings_confirmed"] + event["findings_rejected"] <= event[
        "findings_identified"
    ]


def _parse_events(metrics: IO[str]) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for line in metrics:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _event_timestamp(event: dict[str, object]) -> datetime | None:
    try:
        timestamp = datetime.fromisoformat(str(event["timestamp"]))
    except (KeyError, TypeError, ValueError):
        return None
    return timestamp if timestamp.tzinfo is not None else timestamp.replace(tzinfo=UTC)


def _serialize(event: dict[str, object]) -> str:
    return json.dumps(event, separators=(",", ":"))
