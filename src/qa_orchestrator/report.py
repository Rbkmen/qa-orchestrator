import argparse
import json
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from os import environ
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from qa_orchestrator.events import (
    DEEP_VALUE_COUNTERS,
    LEGACY_MODEL_CALL_COUNTERS,
    MODEL_TOKEN_COUNTERS,
    QA_TASK_TOKEN_COUNTERS,
    SCOPE_COUNTERS,
    SHARED_ORCHESTRATION_COUNTERS,
    STAGE_CALL_COUNTERS,
    STAGE_TOKEN_COUNTERS,
    read_metrics_lines,
    valid_qa_task_metrics,
)


class _StrictReportModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DeepEscalationReport(_StrictReportModel):
    recommended_tasks: int
    by_reason: dict[str, int]


class ModelTokenTotals(_StrictReportModel):
    input: int
    output: int


class ModelTokensReport(_StrictReportModel):
    luna: ModelTokenTotals
    terra_primary: ModelTokenTotals
    sol: ModelTokenTotals
    terra_synthesis: ModelTokenTotals
    complete_measurement_tasks: int


class ScopeReport(_StrictReportModel):
    evidence_packet_tokens: int
    merge_requests: int
    repositories: int


class DeepValueReport(_StrictReportModel):
    measurement_tasks: int
    identified: int
    new_confirmed: int
    rejected: int


class CodeGraphReport(_StrictReportModel):
    tasks: int
    calls: int
    response_tokens: int
    avoided_source_read_tokens: int
    estimated_source_token_savings_pct: float | None


class OrchestrationReport(_StrictReportModel):
    tasks: int
    luna_calls: int
    terra_calls: int
    sol_calls: int
    steps_completed: int
    retries: int


class StageCallsReport(_StrictReportModel):
    triage: int
    primary_review: int
    deep_review: int
    synthesis: int


class StageTokensReport(_StrictReportModel):
    triage: ModelTokenTotals
    primary_review: ModelTokenTotals
    deep_review: ModelTokenTotals
    synthesis: ModelTokenTotals


class QaTasksReport(_StrictReportModel):
    events: int
    outcomes: dict[str, int]
    by_task_type: dict[str, int]
    codegraph_calls: int
    source_mcp_calls: int
    deep_tasks: int
    deep_escalation: DeepEscalationReport
    deep_by_model: dict[str, int]
    deep_by_reasoning: dict[str, int]
    deep_duration_ms: int
    deep_input_tokens: int
    deep_output_tokens: int
    model_tokens: ModelTokensReport
    scope: ScopeReport
    deep_value: DeepValueReport
    findings_identified: int
    findings_confirmed: int
    findings_rejected: int
    repeated_source_reads: int
    codegraph: CodeGraphReport
    source_mcp_response_tokens: int
    complete_token_measurement_tasks: int
    orchestration: OrchestrationReport
    stage_calls: StageCallsReport
    stage_tokens: StageTokensReport


class DataQualityReport(_StrictReportModel):
    task_events: int
    complete_token_measurement_rate: float | None
    complete_model_token_measurement_rate: float | None


class MetricsReport(_StrictReportModel):
    period_days: int = Field(ge=1)
    qa_tasks: QaTasksReport
    data_quality: DataQualityReport


def summarize_events(lines: Iterable[str], days: int = 7) -> dict[str, object]:
    cutoff = datetime.now(UTC) - timedelta(days=days)
    outcomes: Counter[str] = Counter()
    task_types: Counter[str] = Counter()
    deep_models: Counter[str] = Counter()
    deep_reasoning: Counter[str] = Counter()
    deep_escalation_reasons: Counter[str] = Counter()
    totals = Counter()
    stage_token_sources = {
        "triage": ("triage_input_tokens", "triage_output_tokens"),
        "primary_review": ("primary_review_input_tokens", "primary_review_output_tokens"),
        "deep_review": ("deep_input_tokens", "deep_output_tokens"),
        "synthesis": ("synthesis_input_tokens", "synthesis_output_tokens"),
    }

    for line in lines:
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        timestamp = _event_timestamp(event)
        schema_version = event.get("schema_version")
        if (
            event.get("event_type") != "qa_task_outcome"
            or type(schema_version) is not int
            or schema_version not in {1, 2}
            or timestamp is None
            or timestamp < cutoff
            or not valid_qa_task_metrics(event)
        ):
            continue

        outcomes[str(event["outcome"])] += 1
        task_types[str(event["task_type"])] += 1
        totals["events"] += 1
        totals["deep_tasks"] += event["deep_analysis_used"] is True
        deep_escalation_recommended = event.get("deep_escalation_recommended") is True
        totals["deep_escalation_recommended_tasks"] += deep_escalation_recommended
        if deep_escalation_recommended:
            for reason in event.get("deep_escalation_reason_codes", []):
                deep_escalation_reasons[str(reason)] += 1
        totals["codegraph_tasks"] += event["codegraph_calls"] > 0
        totals["complete_token_measurement_tasks"] += QA_TASK_TOKEN_COUNTERS <= event.keys()
        model_token_fields = (
            MODEL_TOKEN_COUNTERS if schema_version == 1 else STAGE_TOKEN_COUNTERS
        )
        totals["complete_model_token_measurement_tasks"] += model_token_fields <= event.keys()
        totals["deep_value_measurement_tasks"] += DEEP_VALUE_COUNTERS <= event.keys()
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
            *SCOPE_COUNTERS,
            *DEEP_VALUE_COUNTERS,
            *SHARED_ORCHESTRATION_COUNTERS,
        ):
            value = event.get(field, 0)
            if type(value) is int and value >= 0:
                totals[field] += value
        if schema_version == 1:
            for field in (*MODEL_TOKEN_COUNTERS, *LEGACY_MODEL_CALL_COUNTERS):
                value = event.get(field, 0)
                if type(value) is int and value >= 0:
                    totals[field] += value
            totals["v1_deep_input_tokens"] += event.get("deep_input_tokens", 0)
            totals["v1_deep_output_tokens"] += event.get("deep_output_tokens", 0)
        else:
            for field in (*STAGE_CALL_COUNTERS, *STAGE_TOKEN_COUNTERS):
                value = event.get(field, 0)
                if type(value) is int and value >= 0:
                    totals[field] += value
            totals["stage_deep_input_tokens"] += event.get("deep_input_tokens", 0)
            totals["stage_deep_output_tokens"] += event.get("deep_output_tokens", 0)

    return {
        "period_days": days,
        "qa_tasks": {
            "events": totals["events"],
            "outcomes": dict(sorted(outcomes.items())),
            "by_task_type": dict(sorted(task_types.items())),
            "codegraph_calls": totals["codegraph_calls"],
            "source_mcp_calls": totals["source_mcp_calls"],
            "deep_tasks": totals["deep_tasks"],
            "deep_escalation": {
                "recommended_tasks": totals["deep_escalation_recommended_tasks"],
                "by_reason": dict(sorted(deep_escalation_reasons.items())),
            },
            "deep_by_model": dict(sorted(deep_models.items())),
            "deep_by_reasoning": dict(sorted(deep_reasoning.items())),
            "deep_duration_ms": totals["deep_duration_ms"],
            "deep_input_tokens": totals["deep_input_tokens"],
            "deep_output_tokens": totals["deep_output_tokens"],
            "model_tokens": {
                "luna": {
                    "input": totals["luna_input_tokens"],
                    "output": totals["luna_output_tokens"],
                },
                "terra_primary": {
                    "input": totals["terra_primary_input_tokens"],
                    "output": totals["terra_primary_output_tokens"],
                },
                "sol": {
                    "input": totals["v1_deep_input_tokens"],
                    "output": totals["v1_deep_output_tokens"],
                },
                "terra_synthesis": {
                    "input": totals["terra_synthesis_input_tokens"],
                    "output": totals["terra_synthesis_output_tokens"],
                },
                "complete_measurement_tasks": totals[
                    "complete_model_token_measurement_tasks"
                ],
            },
            "scope": {
                "evidence_packet_tokens": totals["evidence_packet_tokens"],
                "merge_requests": totals["merge_requests_count"],
                "repositories": totals["repositories_count"],
            },
            "deep_value": {
                "measurement_tasks": totals["deep_value_measurement_tasks"],
                "identified": totals["deep_findings_identified"],
                "new_confirmed": totals["deep_findings_new_confirmed"],
                "rejected": totals["deep_findings_rejected"],
            },
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
            "stage_calls": {
                "triage": totals["triage_calls"],
                "primary_review": totals["primary_review_calls"],
                "deep_review": totals["deep_review_calls"],
                "synthesis": totals["synthesis_calls"],
            },
            "stage_tokens": {
                stage: {
                    "input": (
                        totals["stage_deep_input_tokens"]
                        if stage == "deep_review"
                        else totals[input_field]
                    ),
                    "output": (
                        totals["stage_deep_output_tokens"]
                        if stage == "deep_review"
                        else totals[output_field]
                    ),
                }
                for stage, (input_field, output_field) in stage_token_sources.items()
            },
        },
        "data_quality": {
            "task_events": totals["events"],
            "complete_token_measurement_rate": _coverage_rate(
                totals["complete_token_measurement_tasks"], totals["events"]
            ),
            "complete_model_token_measurement_rate": _coverage_rate(
                totals["complete_model_token_measurement_tasks"], totals["events"]
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
    data_dir = Path(environ.get("QA_ORCHESTRATOR_DATA_DIR", str(Path.home() / ".qa-orchestrator")))
    print(json.dumps(summarize_events(read_metrics_lines(data_dir / "metrics.jsonl"), args.days), indent=2))
