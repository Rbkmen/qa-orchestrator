import json
import sys
from collections import Counter
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from fcntl import LOCK_EX, LOCK_SH, LOCK_UN, flock
from pathlib import Path
from re import fullmatch
from typing import IO, Protocol

from qa_router_mcp.contracts import CanaryFeedbackReceipt, GenerationStats, QaTaskOutcomeReceipt
from qa_router_mcp.quality import QualityGate, assess_quality

CANARY_TOOL_TARGETS = {
    "test_cases": 15,
    "log_summary": 10,
    "automation_skeleton": 10,
    "text_summary": 10,
    "rewrite": 10,
    "translation": 10,
    "short_explanation": 10,
    "review_checklist": 10,
}
REVIEWABLE_TOOLS = set(CANARY_TOOL_TARGETS)
CANARY_TARGET = sum(CANARY_TOOL_TARGETS.values())
CANARY_VERDICTS = {"accepted", "edited", "rejected"}
CANARY_REASONS = {"none", "factual", "coverage", "format", "too_verbose", "other"}
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
    "qwen_edits",
    "repeated_source_reads",
}
QA_TASK_TOKEN_COUNTERS = {
    "codegraph_response_tokens",
    "source_mcp_response_tokens",
    "avoided_source_read_tokens",
}


class EventSink(Protocol):
    def emit(
        self,
        tool: str,
        outcome: str,
        duration_ms: float,
        error_category: str | None,
        input_chars: int = 0,
        stats: GenerationStats | None = None,
        validation_repair: bool = False,
        *,
        model: str = "unknown",
        profile_version: str = "legacy",
        source: str = "legacy",
        estimated_prompt_tokens: int = 0,
        context_tokens: int = 0,
        draft_id: str | None = None,
        quality_status: str = "canary",
        shadow_evaluation_required: bool = False,
        tokenization_ms: float = 0,
        model_load_ms: float = 0,
        generation_ms: float = 0,
        validation_ms: float = 0,
        repair_ms: float = 0,
        cold_start_likely: bool = False,
        phase_latency_available: bool = False,
    ) -> str | None: ...

    def record_feedback(
        self,
        draft_id: str,
        verdict: str,
        reason: str,
    ) -> CanaryFeedbackReceipt: ...

    def record_qa_task_outcome(self, event: dict[str, object]) -> QaTaskOutcomeReceipt: ...

    def quality_gate(self, tool: str, profile_version: str) -> QualityGate: ...


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

    def emit(
        self,
        tool: str,
        outcome: str,
        duration_ms: float,
        error_category: str | None,
        input_chars: int = 0,
        stats: GenerationStats | None = None,
        validation_repair: bool = False,
        *,
        model: str = "unknown",
        profile_version: str = "legacy",
        source: str = "legacy",
        estimated_prompt_tokens: int = 0,
        context_tokens: int = 0,
        draft_id: str | None = None,
        quality_status: str = "canary",
        shadow_evaluation_required: bool = False,
        tokenization_ms: float = 0,
        model_load_ms: float = 0,
        generation_ms: float = 0,
        validation_ms: float = 0,
        repair_ms: float = 0,
        cold_start_likely: bool = False,
        phase_latency_available: bool = False,
    ) -> str | None:
        usage = stats or GenerationStats()
        event: dict[str, object] = {
            "schema_version": 7,
            "timestamp": datetime.now(UTC).isoformat(),
            "tool": tool,
            "model": model,
            "profile_version": profile_version,
            "source": source,
            "outcome": outcome,
            "next_route": "local_draft" if outcome == "ok" else "host_agent",
            "duration_ms": round(duration_ms, 2),
            "error_category": error_category,
            "input_chars": input_chars,
            "estimated_prompt_tokens": estimated_prompt_tokens,
            "context_tokens": context_tokens,
            "prompt_tokens": usage.prompt_tokens,
            "output_tokens": usage.output_tokens,
            "requests": usage.requests,
            "token_usage_available": usage.usage_available is True,
            "truncated": usage.truncated,
            "validation_repair": validation_repair,
            "quality_status": quality_status,
            "shadow_evaluation_required": shadow_evaluation_required,
            "tokenization_ms": round(tokenization_ms, 2),
            "model_load_ms": round(model_load_ms, 2),
            "generation_ms": round(generation_ms, 2),
            "validation_ms": round(validation_ms, 2),
            "repair_ms": round(repair_ms, 2),
            "cold_start_likely": cold_start_likely,
            "phase_latency_available": phase_latency_available,
        }
        if draft_id is None:
            self._write(event)
            return None
        return self._write_canary_candidate(event, draft_id)

    def record_feedback(
        self,
        draft_id: str,
        verdict: str,
        reason: str,
    ) -> CanaryFeedbackReceipt:
        if self.path is None:
            return self._receipt("unavailable", 0)
        if not _valid_feedback_values(verdict, reason):
            return self._receipt("invalid", 0)
        try:
            with self._locked_events() as (metrics, events):
                issued = _issued_canary_drafts(events)
                draft = issued.get(draft_id)
                if draft is None:
                    return self._receipt("not_found", len(validated_canary_feedback(events)))
                profile_version = str(draft["profile_version"])
                feedback = validated_canary_feedback(events, profile_version)
                feedback_ids = {str(event["draft_id"]) for event in feedback}
                if draft_id in feedback_ids:
                    return self._receipt("duplicate", len(feedback))
                tool = str(draft["tool"])
                progress = Counter(str(event["tool"]) for event in feedback)
                shadow_sample = draft.get("shadow_evaluation_required") is True
                if not shadow_sample and (
                    tool not in CANARY_TOOL_TARGETS or progress[tool] >= CANARY_TOOL_TARGETS[tool]
                ):
                    return self._receipt("complete", len(feedback))
                event = {
                    "schema_version": 4,
                    "event_type": "canary_feedback",
                    "timestamp": datetime.now(UTC).isoformat(),
                    "tool": tool,
                    "draft_id": draft_id,
                    "profile_version": draft["profile_version"],
                    "source": "interactive",
                    "verdict": verdict,
                    "reason": reason,
                }
                self._append_locked(metrics, events, event)
                print(_serialize(event), file=sys.stderr, flush=True)
                return self._receipt("recorded", len(feedback) + 1)
        except OSError:
            return self._receipt("unavailable", 0)

    def record_qa_task_outcome(self, event: dict[str, object]) -> QaTaskOutcomeReceipt:
        if self.path is None:
            return QaTaskOutcomeReceipt(status="unavailable")
        payload = {
            **event,
            "schema_version": 7,
            "event_type": "qa_task_outcome",
            "timestamp": datetime.now(UTC).isoformat(),
        }
        return QaTaskOutcomeReceipt(status="recorded" if self._write(payload) else "unavailable")

    def quality_gate(self, tool: str, profile_version: str) -> QualityGate:
        if self.path is None:
            return assess_quality(tool, [])
        try:
            lines = read_metrics_lines(self.path)
        except OSError:
            return QualityGate("paused", 0, None, None)
        events = _parse_events_strict(lines)
        if events is None:
            return QualityGate("paused", 0, None, None)
        return assess_quality(tool, validated_canary_feedback(events, profile_version))

    @staticmethod
    def _receipt(status: str, feedback_count: int) -> CanaryFeedbackReceipt:
        return CanaryFeedbackReceipt(
            status=status,
            feedback_count=feedback_count,
            target=CANARY_TARGET,
        )

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

    def _append_locked(
        self,
        metrics: IO[str],
        events: list[dict[str, object]],
        event: dict[str, object],
    ) -> None:
        cutoff = datetime.now(UTC) - timedelta(days=self.retention_days)
        retained = []
        for item in [*events, event]:
            timestamp = _event_timestamp(item)
            if timestamp is not None and timestamp >= cutoff:
                retained.append(item)
        metrics.seek(0)
        metrics.truncate()
        metrics.write("".join(_serialize(item) + "\n" for item in retained[-self.max_events :]))
        metrics.flush()

    def _write(self, event: dict[str, object]) -> bool:
        line = _serialize(event)
        print(line, file=sys.stderr, flush=True)
        if self.path is None:
            return True
        try:
            with self._locked_events() as (metrics, events):
                self._append_locked(metrics, events, event)
            return True
        except OSError:
            return False

    def _write_canary_candidate(
        self,
        event: dict[str, object],
        draft_id: str,
    ) -> str | None:
        if self.path is None:
            return None
        try:
            with self._locked_events() as (metrics, events):
                tool = str(event["tool"])
                profile_version = str(event["profile_version"])
                reviewed = Counter(
                    str(review["tool"])
                    for review in validated_canary_feedback(events, profile_version)
                )
                shadow_sample = event.get("shadow_evaluation_required") is True
                if not shadow_sample and (
                    tool not in CANARY_TOOL_TARGETS or reviewed[tool] >= CANARY_TOOL_TARGETS[tool]
                ):
                    self._append_locked(metrics, events, event)
                    print(_serialize(event), file=sys.stderr, flush=True)
                    return None
                event["draft_id"] = draft_id
                self._append_locked(metrics, events, event)
                print(_serialize(event), file=sys.stderr, flush=True)
                return draft_id
        except OSError:
            return None


def read_metrics_lines(path: Path) -> list[str]:
    """Read a stable metrics snapshot while writers hold an exclusive lock."""
    try:
        with path.open(encoding="utf-8") as metrics:
            flock(metrics.fileno(), LOCK_SH)
            try:
                return metrics.read().splitlines()
            finally:
                flock(metrics.fileno(), LOCK_UN)
    except FileNotFoundError:
        return []


def validated_canary_feedback(
    events: list[dict[str, object]],
    profile_version: str | None = None,
) -> list[dict[str, object]]:
    issued = _issued_canary_drafts(events)
    seen: set[str] = set()
    valid: list[dict[str, object]] = []
    for event in events:
        if (
            event.get("schema_version") != 4
            or event.get("event_type") != "canary_feedback"
            or event.get("source") != "interactive"
        ):
            continue
        draft_id = event.get("draft_id")
        draft = issued.get(draft_id) if isinstance(draft_id, str) else None
        if draft is None or draft_id in seen:
            continue
        if event.get("tool") != draft.get("tool"):
            continue
        if event.get("profile_version") != draft.get("profile_version"):
            continue
        if profile_version is not None and event.get("profile_version") != profile_version:
            continue
        verdict = event.get("verdict")
        reason = event.get("reason")
        if not isinstance(verdict, str) or not isinstance(reason, str):
            continue
        if not _valid_feedback_values(verdict, reason):
            continue
        seen.add(draft_id)
        valid.append(event)
    return valid


def _issued_canary_drafts(
    events: list[dict[str, object]],
    profile_version: str | None = None,
) -> dict[str, dict[str, object]]:
    issued: dict[str, dict[str, object]] = {}
    for event in events:
        draft_id = event.get("draft_id")
        tool = event.get("tool")
        if (
            isinstance(draft_id, str)
            and fullmatch(r"[0-9a-f]{32}", draft_id)
            and isinstance(tool, str)
            and tool in REVIEWABLE_TOOLS
            and event.get("outcome") == "ok"
            and event.get("source") == "interactive"
            and isinstance(event.get("profile_version"), str)
            and (profile_version is None or event.get("profile_version") == profile_version)
        ):
            issued[draft_id] = event
    return issued


def _parse_events(metrics: Iterable[str]) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for line in metrics:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _parse_events_strict(metrics: Iterable[str]) -> list[dict[str, object]] | None:
    events: list[dict[str, object]] = []
    for line in metrics:
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(event, dict):
            return None
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


def _valid_feedback_values(verdict: str, reason: str) -> bool:
    return (
        verdict in CANARY_VERDICTS
        and reason in CANARY_REASONS
        and ((verdict == "accepted") == (reason == "none"))
    )


def valid_qa_task_metrics(event: dict[str, object]) -> bool:
    if event.get("task_type") not in QA_TASK_TYPES or event.get("outcome") not in QA_TASK_OUTCOMES:
        return False
    if type(event.get("qwen_used")) is not bool:
        return False
    legacy_sol = event.get("sol_used")
    deep_used = event.get("deep_analysis_used", legacy_sol)
    if type(deep_used) is not bool:
        return False
    if legacy_sol is not None and type(legacy_sol) is not bool:
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
        or event["deep_reasoning"]
        not in {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
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
    ] and (event["qwen_used"] or event["qwen_edits"] == 0)
