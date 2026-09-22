import json
from datetime import UTC, datetime

from qa_orchestrator.report import summarize_events


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
                "deep_escalation_recommended": True,
                "deep_escalation_reason_codes": [
                    "high_risk_domain",
                    "evidence_gap",
                ],
                "deep_model": "gpt-5.6-sol",
                "deep_reasoning": "high",
                "deep_duration_ms": 120,
                "deep_input_tokens": 20,
                "deep_output_tokens": 30,
                "deep_findings_identified": 2,
                "deep_findings_new_confirmed": 1,
                "deep_findings_rejected": 1,
                "luna_input_tokens": 100,
                "luna_output_tokens": 25,
                "terra_primary_input_tokens": 240,
                "terra_primary_output_tokens": 80,
                "terra_synthesis_input_tokens": 120,
                "terra_synthesis_output_tokens": 40,
                "evidence_packet_tokens": 180,
                "merge_requests_count": 2,
                "repositories_count": 2,
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
                "schema_version": 1,
                "event_type": "qa_task_outcome",
                "timestamp": timestamp,
                "task_type": "ordinary_review",
                "outcome": "completed",
                "legacy_metric": True,
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
    assert report["qa_tasks"]["deep_escalation"] == {
        "recommended_tasks": 1,
        "by_reason": {
            "evidence_gap": 1,
            "high_risk_domain": 1,
        },
    }
    assert report["qa_tasks"]["model_tokens"] == {
        "luna": {"input": 100, "output": 25},
        "terra_primary": {"input": 240, "output": 80},
        "sol": {"input": 20, "output": 30},
        "terra_synthesis": {"input": 120, "output": 40},
        "complete_measurement_tasks": 1,
    }
    assert report["qa_tasks"]["scope"] == {
        "evidence_packet_tokens": 180,
        "merge_requests": 2,
        "repositories": 2,
    }
    assert report["qa_tasks"]["deep_value"] == {
        "measurement_tasks": 1,
        "identified": 2,
        "new_confirmed": 1,
        "rejected": 1,
    }
    assert report["qa_tasks"]["deep_by_model"] == {"gpt-5.6-sol": 1}
    assert report["qa_tasks"]["findings_confirmed"] == 1
    assert report["qa_tasks"]["codegraph"]["calls"] == 1
    assert "by_model" not in report


def test_report_aggregates_orchestration_counters():
    line = json.dumps(
        {
            "schema_version": 1,
            "event_type": "qa_task_outcome",
            "timestamp": datetime.now(UTC).isoformat(),
            "task_type": "ordinary_review",
            "outcome": "completed",
            "deep_analysis_used": True,
            "deep_model": "gpt-5.6-sol",
            "deep_reasoning": "high",
            "codegraph_calls": 1,
            "source_mcp_calls": 2,
            "findings_identified": 1,
            "findings_confirmed": 1,
            "findings_rejected": 0,
            "repeated_source_reads": 0,
            "orchestration_used": True,
            "luna_calls": 1,
            "terra_calls": 2,
            "sol_calls": 1,
            "orchestration_steps_completed": 4,
            "orchestration_retries": 0,
        }
    )

    report = summarize_events([line])

    assert report["qa_tasks"]["orchestration"] == {
        "tasks": 1,
        "luna_calls": 1,
        "terra_calls": 2,
        "sol_calls": 1,
        "steps_completed": 4,
        "retries": 0,
    }


def test_report_skips_incomplete_orchestration_event_without_crashing():
    line = json.dumps(
        {
            "schema_version": 1,
            "event_type": "qa_task_outcome",
            "timestamp": datetime.now(UTC).isoformat(),
            "task_type": "ordinary_review",
            "outcome": "completed",
            "deep_analysis_used": False,
            "codegraph_calls": 0,
            "source_mcp_calls": 0,
            "findings_identified": 0,
            "findings_confirmed": 0,
            "findings_rejected": 0,
            "repeated_source_reads": 0,
            "orchestration_used": True,
        }
    )

    report = summarize_events([line])

    assert report["qa_tasks"]["events"] == 0
