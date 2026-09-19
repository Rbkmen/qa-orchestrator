import json
from datetime import UTC, datetime

from qa_router_mcp.report import summarize_events


def test_report_aggregates_only_model_free_task_outcomes():
    timestamp = datetime.now(UTC).isoformat()
    lines = [
        json.dumps(
            {
                "schema_version": 1,
                "event_type": "qa_task_outcome",
                "timestamp": timestamp,
                "task_type": "ordinary_review",
                "outcome": "completed",
                "deep_analysis_used": True,
                "deep_model": "gpt-5.6-sol",
                "deep_reasoning": "high",
                "deep_duration_ms": 120,
                "deep_input_tokens": 20,
                "deep_output_tokens": 30,
                "codegraph_calls": 1,
                "source_mcp_calls": 2,
                "findings_identified": 2,
                "findings_confirmed": 1,
                "findings_rejected": 1,
                "repeated_source_reads": 0,
                "codegraph_response_tokens": 10,
                "source_mcp_response_tokens": 20,
                "avoided_source_read_tokens": 5,
            }
        ),
        json.dumps(
            {
                "schema_version": 1,
                "event_type": "qa_task_outcome",
                "timestamp": timestamp,
                "task_type": "ordinary_review",
                "outcome": "completed",
                "deep_analysis_used": False,
                "codegraph_calls": 0,
                "source_mcp_calls": 1,
                "findings_identified": 0,
                "findings_confirmed": 0,
                "findings_rejected": 0,
                "repeated_source_reads": 0,
            }
        ),
        json.dumps(
            {
                "schema_version": 7,
                "event_type": "qa_task_outcome",
                "timestamp": timestamp,
                "task_type": "ordinary_review",
                "outcome": "completed",
                "qwen_used": True,
                "deep_analysis_used": False,
                "codegraph_calls": 0,
                "source_mcp_calls": 0,
                "findings_identified": 0,
                "findings_confirmed": 0,
                "findings_rejected": 0,
                "repeated_source_reads": 0,
            }
        ),
    ]

    report = summarize_events(lines)

    assert report["qa_tasks"]["events"] == 2
    assert report["qa_tasks"]["outcomes"] == {"completed": 2}
    assert report["qa_tasks"]["by_task_type"] == {"ordinary_review": 2}
    assert report["qa_tasks"]["deep_tasks"] == 1
    assert report["qa_tasks"]["deep_by_model"] == {"gpt-5.6-sol": 1}
    assert report["qa_tasks"]["findings_confirmed"] == 1
    assert report["qa_tasks"]["codegraph"]["calls"] == 1
    assert "qwen_tasks" not in report["qa_tasks"]
    assert "by_model" not in report
