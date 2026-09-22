import json
import os
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from fcntl import LOCK_EX, LOCK_SH, LOCK_UN, flock
from pathlib import Path
from tempfile import mkstemp
from typing import IO, Protocol

from qa_orchestrator.contracts import QaTaskOutcomeReceipt

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
MODEL_TOKEN_COUNTERS = {
    "luna_input_tokens",
    "luna_output_tokens",
    "terra_primary_input_tokens",
    "terra_primary_output_tokens",
    "terra_synthesis_input_tokens",
    "terra_synthesis_output_tokens",
}
SCOPE_COUNTERS = {
    "evidence_packet_tokens",
    "merge_requests_count",
    "repositories_count",
}
DEEP_VALUE_COUNTERS = {
    "deep_findings_identified",
    "deep_findings_new_confirmed",
    "deep_findings_rejected",
}
ORCHESTRATION_COUNTERS = {
    "luna_calls",
    "terra_calls",
    "sol_calls",
    "orchestration_steps_completed",
    "orchestration_retries",
}
DEEP_REASONING = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
DEEP_MODELS = {"gpt-5.6-sol"}
DEEP_ESCALATION_REASON_CODES = {
    "evidence_gap",
    "cross_repository",
    "security_sensitive",
    "payment_sensitive",
    "fraud_sensitive",
    "root_cause",
    "high_blast_radius",
    "high_risk_domain",
    "evidence_conflict",
    "non_reproducible",
}
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
    "deep_escalation_recommended",
    "deep_escalation_reason_codes",
    "orchestration_used",
    *QA_TASK_COUNTERS,
    *QA_TASK_TOKEN_COUNTERS,
    *MODEL_TOKEN_COUNTERS,
    *SCOPE_COUNTERS,
    *DEEP_VALUE_COUNTERS,
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
            "deep_escalation_recommended",
            "deep_escalation_reason_codes",
            "orchestration_used",
            *QA_TASK_COUNTERS,
            *QA_TASK_TOKEN_COUNTERS,
            *MODEL_TOKEN_COUNTERS,
            *SCOPE_COUNTERS,
            *DEEP_VALUE_COUNTERS,
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
    def _locked_events(self) -> Iterator[list[dict[str, object]]]:
        if self.path is None:
            raise OSError("metrics path is unavailable")
        _prepare_metrics_directory(self.path.parent)
        with _locked_path(self.path, LOCK_EX):
            try:
                with self.path.open(encoding="utf-8", errors="replace") as metrics:
                    events = _parse_events(metrics)
            except FileNotFoundError:
                events = []
            yield events

    def _write(self, event: dict[str, object]) -> bool:
        line = _serialize(event)
        print(line, file=sys.stderr, flush=True)
        try:
            with self._locked_events() as events:
                cutoff = datetime.now(UTC) - timedelta(days=self.retention_days)
                retained = [
                    item
                    for item in [*events, event]
                    if (timestamp := _event_timestamp(item)) is not None and timestamp >= cutoff
                ]
                _atomic_write_events(self.path, retained[-self.max_events :])
            return True
        except OSError:
            return False


def read_metrics_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        with _locked_path(path, LOCK_SH), path.open(
            encoding="utf-8", errors="replace"
        ) as metrics:
            return metrics.read().splitlines()
    except FileNotFoundError:
        return []


def _prepare_metrics_directory(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=False, mode=0o700)
    except FileExistsError:
        if path.is_symlink() or not path.is_dir():
            raise OSError("metrics directory must be a real directory")
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise OSError("metrics directory must be private")
        return
    path.chmod(0o700)


@contextmanager
def _locked_path(path: Path, operation: int) -> Iterator[None]:
    lock_path = path.with_name(f".{path.name}.lock")
    lock_mode = "a+" if operation == LOCK_EX else "r"
    try:
        lock = lock_path.open(lock_mode, encoding="utf-8")
    except FileNotFoundError:
        if operation == LOCK_SH:
            # Writers use atomic replacement, so an absent lock file is safe for a read.
            yield
            return
        raise
    with lock:
        if operation == LOCK_EX:
            lock_path.chmod(0o600)
        flock(lock.fileno(), operation)
        try:
            yield
        finally:
            flock(lock.fileno(), LOCK_UN)


def _atomic_write_events(path: Path, events: list[dict[str, object]]) -> None:
    file_descriptor, temporary_name = mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as temporary:
            os.fchmod(temporary.fileno(), 0o600)
            temporary.write("".join(_serialize(item) + "\n" for item in events))
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


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
    escalation_fields_present = {
        "deep_escalation_recommended",
        "deep_escalation_reason_codes",
    } & event.keys()
    if escalation_fields_present and escalation_fields_present != {
        "deep_escalation_recommended",
        "deep_escalation_reason_codes",
    }:
        return False
    if escalation_fields_present:
        recommended = event["deep_escalation_recommended"]
        reasons = event["deep_escalation_reason_codes"]
        if type(recommended) is not bool or not isinstance(reasons, list):
            return False
        if any(
            not isinstance(reason, str) or reason not in DEEP_ESCALATION_REASON_CODES
            for reason in reasons
        ) or len(reasons) != len(set(reasons)):
            return False
        if recommended is False and reasons:
            return False
        if recommended is True and not reasons:
            return False
    if "deep_model" in event and (
        not deep_used
        or not isinstance(event["deep_model"], str)
        or event["deep_model"] not in DEEP_MODELS
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
        for field in (*MODEL_TOKEN_COUNTERS, *SCOPE_COUNTERS, *DEEP_VALUE_COUNTERS)
    ):
        return False
    if not deep_used and any(event.get(field, 0) > 0 for field in DEEP_VALUE_COUNTERS):
        return False
    if event.get("deep_findings_new_confirmed", 0) + event.get(
        "deep_findings_rejected", 0
    ) > event.get("deep_findings_identified", 0):
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
    if orchestration_used and (event.get("sol_calls", 0) > 0) != deep_used:
        return False
    if orchestration_used and event["outcome"] == "completed" and (
        event["luna_calls"] < 1
        or event["terra_calls"] < 2
        or event["orchestration_steps_completed"] < 3
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


def qa_task_metric_errors(event: dict[str, object]) -> list[str]:
    """Return actionable explanations for the common metric invariants."""
    errors: list[str] = []

    if event.get("orchestration_used") is True and event.get("outcome") == "completed":
        for field, minimum in (
            ("luna_calls", 1),
            ("terra_calls", 2),
            ("orchestration_steps_completed", 3),
        ):
            value = event.get(field, 0)
            if type(value) is int and value < minimum:
                errors.append(f"{field} must be >= {minimum}")

    if (
        type(event.get("findings_confirmed")) is int
        and type(event.get("findings_rejected")) is int
        and type(event.get("findings_identified")) is int
        and event["findings_confirmed"] + event["findings_rejected"]
        > event["findings_identified"]
    ):
        errors.append("findings_confirmed + findings_rejected must be <= findings_identified")

    if (
        type(event.get("repeated_source_reads")) is int
        and type(event.get("source_mcp_calls")) is int
        and event["repeated_source_reads"] > event["source_mcp_calls"]
    ):
        errors.append("repeated_source_reads must be <= source_mcp_calls")

    if event.get("orchestration_used") is True and (
        (event.get("sol_calls", 0) > 0) != (event.get("deep_analysis_used") is True)
    ):
        errors.append("sol_calls must be positive exactly when deep_analysis_used is true")

    return errors or ["one or more metric values have an invalid type or value"]


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
