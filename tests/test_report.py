import json
from datetime import UTC, datetime

from qa_router_mcp.events import CANARY_TARGET, CANARY_TOOL_TARGETS
from qa_router_mcp.report import main, summarize_events


def test_weekly_report_aggregates_metadata_only():
    timestamp = datetime.now(UTC).isoformat()
    lines = [
        json.dumps(
            {
                "timestamp": timestamp,
                "tool": "test_cases",
                "outcome": "ok",
                "prompt_tokens": 100,
                "output_tokens": 50,
                "requests": 1,
                "duration_ms": 10.0,
                "validation_repair": False,
                "truncated": False,
            }
        ),
        json.dumps(
            {
                "schema_version": 2,
                "timestamp": timestamp,
                "tool": "test_cases",
                "outcome": "fallback",
                "model": "qwen/qwen3.5-9b",
                "profile_version": "router-v2",
                "source": "benchmark",
                "estimated_prompt_tokens": 80,
                "prompt_tokens": 120,
                "output_tokens": 60,
                "requests": 2,
                "duration_ms": 30.0,
                "validation_repair": True,
                "truncated": False,
            }
        ),
        "invalid line",
    ]

    report = summarize_events(lines)

    assert report["events"] == 2
    assert report["outcomes"] == {"fallback": 1, "ok": 1}
    assert report["prompt_tokens"] == 220
    assert report["estimated_prompt_tokens"] == 80
    assert report["output_tokens"] == 110
    assert report["validation_repairs"] == 1
    assert report["duration_ms_p50"] == 10.0
    assert report["duration_ms_p95"] == 30.0
    assert report["by_tool"] == {"test_cases": {"fallback": 1, "ok": 1}}
    assert report["by_source"] == {
        "benchmark": {
            "events": 1,
            "outcomes": {"fallback": 1},
            "estimated_prompt_tokens": 80,
            "prompt_tokens": 120,
            "output_tokens": 60,
            "requests": 2,
            "duration_ms_p50": 30.0,
            "duration_ms_p95": 30.0,
        },
        "legacy": {
            "events": 1,
            "outcomes": {"ok": 1},
            "estimated_prompt_tokens": 0,
            "prompt_tokens": 100,
            "output_tokens": 50,
            "requests": 1,
            "duration_ms_p50": 10.0,
            "duration_ms_p95": 10.0,
        },
    }
    assert report["by_model"]["qwen/qwen3.5-9b"] == report["by_source"]["benchmark"]
    assert report["by_model"]["unknown"] == report["by_source"]["legacy"]
    assert report["by_profile"]["router-v2"] == report["by_source"]["benchmark"]
    assert report["by_profile"]["legacy"] == report["by_source"]["legacy"]


def test_weekly_report_aggregates_phase_latency_shadow_and_quality_status():
    timestamp = datetime.now(UTC).isoformat()
    lines = [
        json.dumps(
            {
                "schema_version": 7,
                "timestamp": timestamp,
                "tool": "test_cases",
                "outcome": "ok",
                "model": "qwen/qwen3.5-9b",
                "profile_version": "router-v10",
                "source": "interactive",
                "duration_ms": 40.0,
                "model_load_ms": 10.0,
                "tokenization_ms": 5.0,
                "generation_ms": 30.0,
                "validation_ms": 2.0,
                "repair_ms": 0.0,
                "phase_latency_available": True,
                "cold_start_likely": True,
                "quality_status": "canary",
                "shadow_evaluation_required": True,
            }
        ),
        json.dumps(
            {
                "schema_version": 7,
                "timestamp": timestamp,
                "tool": "translation",
                "outcome": "ok",
                "model": "qwen/qwen3.5-9b",
                "profile_version": "router-v10",
                "source": "interactive",
                "duration_ms": 20.0,
                "model_load_ms": 0.0,
                "tokenization_ms": 1.0,
                "generation_ms": 15.0,
                "validation_ms": 1.0,
                "repair_ms": 0.0,
                "phase_latency_available": True,
                "cold_start_likely": False,
                "quality_status": "active",
                "shadow_evaluation_required": False,
            }
        ),
    ]

    report = summarize_events(lines)

    assert report["phase_latency_ms"] == {
        "model_load_p50": 0.0,
        "model_load_p95": 10.0,
        "tokenization_p50": 1.0,
        "tokenization_p95": 5.0,
        "generation_p50": 15.0,
        "generation_p95": 30.0,
        "validation_p50": 1.0,
        "validation_p95": 2.0,
        "repair_p50": 0.0,
        "repair_p95": 0.0,
    }
    assert report["cold_start_likely"] == {"false": 1, "true": 1}
    assert report["shadow_evaluations"] == {"requested": 1, "rate": 0.5}
    assert report["quality_statuses"] == {"active": 1, "canary": 1}


def test_report_exposes_data_completeness_for_model_comparisons():
    timestamp = datetime.now(UTC).isoformat()
    lines = [
        json.dumps(
            {
                "schema_version": 7,
                "timestamp": timestamp,
                "tool": "translation",
                "outcome": "ok",
                "prompt_tokens": 10,
                "output_tokens": 5,
                "requests": 1,
                "token_usage_available": True,
                "model_load_ms": 1.0,
                "tokenization_ms": 1.0,
                "generation_ms": 2.0,
                "validation_ms": 1.0,
                "repair_ms": 0.0,
                "phase_latency_available": True,
            }
        ),
        json.dumps(
            {
                "schema_version": 7,
                "timestamp": timestamp,
                "tool": "translation",
                "outcome": "fallback",
                "prompt_tokens": 0,
                "output_tokens": 0,
                "requests": 1,
                "token_usage_available": False,
                "model_load_ms": 0.0,
                "tokenization_ms": 0.0,
                "generation_ms": 0.0,
                "validation_ms": 0.0,
                "repair_ms": 0.0,
                "phase_latency_available": False,
            }
        ),
    ]

    assert summarize_events(lines)["data_quality"] == {
        "generation_events": 2,
        "complete_token_usage_events": 1,
        "complete_phase_latency_events": 1,
        "complete_token_usage_rate": 0.5,
        "complete_phase_latency_rate": 0.5,
    }


def test_weekly_report_separates_canary_feedback_from_generation_events():
    timestamp = datetime.now(UTC).isoformat()
    lines = [
        json.dumps(
            {
                "schema_version": 4,
                "timestamp": timestamp,
                "tool": "test_cases",
                "outcome": "ok",
                "source": "interactive",
                "profile_version": "router-v7",
                "draft_id": "a" * 32,
            }
        ),
        json.dumps(
            {
                "schema_version": 4,
                "event_type": "canary_feedback",
                "timestamp": timestamp,
                "tool": "test_cases",
                "profile_version": "router-v7",
                "draft_id": "a" * 32,
                "source": "interactive",
                "verdict": "edited",
                "reason": "coverage",
            }
        ),
        json.dumps(
            {
                "schema_version": 4,
                "timestamp": timestamp,
                "tool": "translation",
                "outcome": "ok",
                "source": "interactive",
                "profile_version": "router-v7",
                "draft_id": "b" * 32,
            }
        ),
        json.dumps(
            {
                "schema_version": 4,
                "event_type": "canary_feedback",
                "timestamp": timestamp,
                "tool": "translation",
                "profile_version": "router-v7",
                "draft_id": "b" * 32,
                "source": "interactive",
                "verdict": "accepted",
                "reason": "none",
            }
        ),
    ]

    report = summarize_events(lines)

    assert report["events"] == 2
    assert report["outcomes"] == {"ok": 2}
    assert report["canary_feedback"] == {
        "target_per_profile": CANARY_TARGET,
        "targets_by_tool": dict(sorted(CANARY_TOOL_TARGETS.items())),
        "by_profile": {
            "router-v7": {
                "reviews": 2,
                "complete": False,
                "progress_by_tool": {
                    "automation_skeleton": 0,
                    "log_summary": 0,
                    "rewrite": 0,
                    "short_explanation": 0,
                    "review_checklist": 0,
                    "test_cases": 1,
                    "text_summary": 0,
                    "translation": 1,
                },
                "verdicts": {"accepted": 1, "edited": 1},
                "reasons": {"coverage": 1, "none": 1},
                "by_tool": {
                    "test_cases": {"edited": 1},
                    "translation": {"accepted": 1},
                },
            }
        },
    }


def test_canary_feedback_progress_is_lifetime_not_weekly():
    old_timestamp = datetime(2020, 1, 1, tzinfo=UTC).isoformat()
    report = summarize_events(
        [
            json.dumps(
                {
                    "schema_version": 4,
                    "timestamp": old_timestamp,
                    "tool": "test_cases",
                    "outcome": "ok",
                    "source": "interactive",
                    "profile_version": "router-v7",
                    "draft_id": "a" * 32,
                }
            ),
            json.dumps(
                {
                    "schema_version": 4,
                    "event_type": "canary_feedback",
                    "timestamp": old_timestamp,
                    "tool": "test_cases",
                    "profile_version": "router-v7",
                    "draft_id": "a" * 32,
                    "source": "interactive",
                    "verdict": "accepted",
                    "reason": "none",
                }
            ),
        ]
    )

    assert report["events"] == 0
    assert report["canary_feedback"]["by_profile"]["router-v7"]["reviews"] == 1


def test_canary_feedback_keeps_profile_versions_separate():
    timestamp = datetime.now(UTC).isoformat()
    lines = []
    for profile, draft_id, verdict, reason in (
        ("router-v7", "a" * 32, "accepted", "none"),
        ("router-v8", "b" * 32, "rejected", "factual"),
    ):
        lines.extend(
            [
                json.dumps(
                    {
                        "schema_version": 4,
                        "timestamp": timestamp,
                        "tool": "test_cases",
                        "outcome": "ok",
                        "source": "interactive",
                        "profile_version": profile,
                        "draft_id": draft_id,
                    }
                ),
                json.dumps(
                    {
                        "schema_version": 4,
                        "event_type": "canary_feedback",
                        "timestamp": timestamp,
                        "tool": "test_cases",
                        "profile_version": profile,
                        "draft_id": draft_id,
                        "source": "interactive",
                        "verdict": verdict,
                        "reason": reason,
                    }
                ),
            ]
        )

    profiles = summarize_events(lines)["canary_feedback"]["by_profile"]

    assert profiles["router-v7"]["verdicts"] == {"accepted": 1}
    assert profiles["router-v8"]["verdicts"] == {"rejected": 1}


def test_weekly_report_aggregates_qa_task_outcomes_separately():
    timestamp = datetime.now(UTC).isoformat()
    lines = [
        json.dumps(
            {
                "schema_version": 6,
                "event_type": "qa_task_outcome",
                "timestamp": timestamp,
                "task_type": "ordinary_review",
                "outcome": "completed",
                "codegraph_calls": 2,
                "source_mcp_calls": 7,
                "qwen_used": True,
                "sol_used": False,
                "findings_identified": 3,
                "findings_confirmed": 2,
                "findings_rejected": 1,
                "qwen_edits": 1,
                "repeated_source_reads": 0,
            }
        ),
        json.dumps(
            {
                "schema_version": 6,
                "event_type": "qa_task_outcome",
                "timestamp": timestamp,
                "task_type": "qa_planning",
                "outcome": "partial",
                "codegraph_calls": 0,
                "source_mcp_calls": 3,
                "qwen_used": False,
                "sol_used": True,
                "findings_identified": 1,
                "findings_confirmed": 1,
                "findings_rejected": 0,
                "qwen_edits": 0,
                "repeated_source_reads": 1,
            }
        ),
    ]

    report = summarize_events(lines)

    assert report["events"] == 0
    assert report["qa_tasks"] == {
        "events": 2,
        "outcomes": {"completed": 1, "partial": 1},
        "by_task_type": {"ordinary_review": 1, "qa_planning": 1},
        "codegraph_calls": 2,
        "source_mcp_calls": 10,
        "qwen_tasks": 1,
        "sol_tasks": 1,
        "deep_tasks": 1,
        "deep_by_model": {"unknown": 1},
        "deep_by_reasoning": {"unknown": 1},
        "deep_duration_ms": 0,
        "deep_input_tokens": 0,
        "deep_output_tokens": 0,
        "findings_identified": 4,
        "findings_confirmed": 3,
        "findings_rejected": 1,
        "qwen_edits": 1,
        "repeated_source_reads": 1,
        "codegraph": {
            "tasks": 1,
            "calls": 2,
            "response_tokens": 0,
            "avoided_source_read_tokens": 0,
            "estimated_source_token_savings_pct": None,
        },
        "source_mcp_response_tokens": 0,
        "complete_token_measurement_tasks": 0,
    }


def test_weekly_report_ignores_invalid_qa_task_outcomes():
    timestamp = datetime.now(UTC).isoformat()
    invalid = {
        "schema_version": 6,
        "event_type": "qa_task_outcome",
        "timestamp": timestamp,
        "task_type": "ordinary_review",
        "outcome": "completed",
        "codegraph_calls": 1,
        "source_mcp_calls": 0,
        "qwen_used": False,
        "sol_used": False,
        "findings_identified": 0,
        "findings_confirmed": 1,
        "findings_rejected": 0,
        "qwen_edits": 0,
        "repeated_source_reads": 0,
    }

    report = summarize_events([json.dumps(invalid)])

    assert report["events"] == 0
    assert report["qa_tasks"]["events"] == 0


def test_weekly_report_ignores_naive_timestamps():
    report = summarize_events(
        [json.dumps({"timestamp": "2026-09-04T12:00:00", "tool": "rewrite", "outcome": "ok"})]
    )

    assert report["events"] == 0


def test_report_cli_honors_days_argument(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("QA_ROUTER_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["qa-router-report", "--days", "30"])

    main()

    assert json.loads(capsys.readouterr().out)["period_days"] == 30


def test_report_aggregates_model_neutral_deep_metrics():
    event = {
        "schema_version": 7,
        "event_type": "qa_task_outcome",
        "timestamp": datetime.now(UTC).isoformat(),
        "task_type": "ordinary_review",
        "outcome": "completed",
        "codegraph_calls": 0,
        "source_mcp_calls": 0,
        "qwen_used": False,
        "deep_analysis_used": True,
        "deep_model": "gpt-6-astra",
        "deep_reasoning": "medium",
        "deep_duration_ms": 1200,
        "deep_input_tokens": 300,
        "deep_output_tokens": 100,
        "findings_identified": 1,
        "findings_confirmed": 1,
        "findings_rejected": 0,
        "qwen_edits": 0,
        "repeated_source_reads": 0,
    }

    deep = summarize_events([json.dumps(event)])["qa_tasks"]

    assert deep["deep_tasks"] == 1
    assert deep["deep_by_model"] == {"gpt-6-astra": 1}
    assert deep["deep_by_reasoning"] == {"medium": 1}
    assert deep["deep_duration_ms"] == 1200
    assert deep["deep_input_tokens"] == 300
    assert deep["deep_output_tokens"] == 100
