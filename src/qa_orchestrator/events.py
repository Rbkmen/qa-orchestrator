import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from fcntl import LOCK_EX, LOCK_SH, LOCK_UN, flock
from pathlib import Path
from tempfile import mkstemp
from typing import IO, Protocol

from qa_orchestrator.contracts import QaTaskOutcomeReceipt

QA_TASK_TYPES = frozenset({
    "ordinary_review",
    "widget_review",
    "epic_analysis",
    "requirements_analysis",
    "qa_planning",
    "autotest_implementation",
    "other",
})
DISTRIBUTION_SCHEMA_VERSION = 3
DISTRIBUTION_EVENT_TYPE = "qa_task_distribution"
LEGACY_OUTCOME_SCHEMA_VERSIONS = frozenset({1, 2})
LEGACY_OUTCOME_EVENT_TYPE = "qa_task_outcome"
DISTRIBUTION_EVENT_FIELDS = {
    "schema_version",
    "event_type",
    "timestamp",
    "task_type",
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

    def sanitize_existing_records(self) -> bool:
        """Rewrite stored events to the task-distribution-only schema."""
        if self.path is None:
            return True
        try:
            validate_metrics_storage(self.path)
            if not self.path.exists():
                return True
            with self._locked_events() as events:
                cutoff = datetime.now(UTC) - timedelta(days=self.retention_days)
                retained = []
                for item in events:
                    distribution_event = normalize_task_distribution_event(item)
                    if distribution_event is None:
                        continue
                    timestamp = _event_timestamp(distribution_event)
                    if timestamp is not None and timestamp >= cutoff:
                        retained.append(distribution_event)
                _atomic_write_events(self.path, retained[-self.max_events :])
            return True
        except OSError:
            return False

    def record_qa_task_outcome(self, event: dict[str, object]) -> QaTaskOutcomeReceipt:
        if self.path is None:
            return QaTaskOutcomeReceipt(status="unavailable")
        payload = {
            "schema_version": DISTRIBUTION_SCHEMA_VERSION,
            "event_type": DISTRIBUTION_EVENT_TYPE,
            "timestamp": datetime.now(UTC).isoformat(),
            "task_type": event.get("task_type"),
        }
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
        normalized_event = normalize_task_distribution_event(event)
        if normalized_event is None:
            return False
        try:
            with self._locked_events() as events:
                cutoff = datetime.now(UTC) - timedelta(days=self.retention_days)
                retained = []
                for item in [*events, normalized_event]:
                    distribution_event = normalize_task_distribution_event(item)
                    if distribution_event is None:
                        continue
                    timestamp = _event_timestamp(distribution_event)
                    if timestamp is not None and timestamp >= cutoff:
                        retained.append(distribution_event)
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
        _validate_existing_metrics_directory(path)
        return
    path.chmod(0o700)
    _validate_existing_metrics_directory(path)


def validate_metrics_storage(path: Path) -> None:
    """Check metrics path safety and writability without creating or changing files."""

    directory = path.parent
    if directory.is_symlink():
        raise OSError("metrics directory must be a real directory")
    if directory.exists():
        _validate_existing_metrics_directory(directory)
    else:
        candidate = directory
        while (
            not candidate.exists()
            and not candidate.is_symlink()
            and candidate != candidate.parent
        ):
            candidate = candidate.parent
        if candidate == directory and candidate.is_symlink():
            raise OSError("metrics directory must be a real directory")
        if not candidate.is_dir():
            raise OSError("metrics directory parent must be a directory")
        if not os.access(candidate, os.W_OK | os.X_OK):
            raise OSError("metrics directory parent must be writable")

    if path.is_symlink():
        raise OSError("metrics file must not be a symlink")
    if path.exists():
        if not path.is_file():
            raise OSError("metrics path must be a regular file")
        if not os.access(path, os.R_OK):
            raise OSError("metrics file must be readable")


def _validate_existing_metrics_directory(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise OSError("metrics directory must be a real directory")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise OSError("metrics directory must be private")
    if not os.access(path, os.W_OK | os.X_OK):
        raise OSError("metrics directory must be writable")


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


def valid_task_distribution_event(event: dict[str, object]) -> bool:
    return (
        set(event) == DISTRIBUTION_EVENT_FIELDS
        and type(event.get("schema_version")) is int
        and event["schema_version"] == DISTRIBUTION_SCHEMA_VERSION
        and event.get("event_type") == DISTRIBUTION_EVENT_TYPE
        and isinstance(event.get("task_type"), str)
        and event["task_type"] in QA_TASK_TYPES
        and _event_timestamp(event) is not None
    )


def normalize_task_distribution_event(event: dict[str, object]) -> dict[str, object] | None:
    if valid_task_distribution_event(event):
        return event
    legacy_schema_version = event.get("schema_version", 1)
    if (
        type(legacy_schema_version) is not int
        or legacy_schema_version not in LEGACY_OUTCOME_SCHEMA_VERSIONS
        or event.get("event_type") != LEGACY_OUTCOME_EVENT_TYPE
        or not isinstance(event.get("task_type"), str)
        or event["task_type"] not in QA_TASK_TYPES
        or (timestamp := _event_timestamp(event)) is None
    ):
        return None
    return {
        "schema_version": DISTRIBUTION_SCHEMA_VERSION,
        "event_type": DISTRIBUTION_EVENT_TYPE,
        "timestamp": timestamp.isoformat(),
        "task_type": event["task_type"],
    }


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
