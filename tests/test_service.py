import json

import pytest

from qa_router_mcp.contracts import ReviewAgent
from qa_router_mcp.service import RouterService


def test_service_resolves_all_review_profiles(tmp_path):
    service = RouterService.from_settings(data_dir=tmp_path)

    routes = [service.prepare_review_route(profile) for profile in ReviewAgent]

    assert [route.profile for route in routes] == list(ReviewAgent)
    assert all(route.read_only and route.host_owns_decisions for route in routes)


def test_service_rejects_unknown_review_profile(tmp_path):
    service = RouterService.from_settings(data_dir=tmp_path)

    with pytest.raises(ValueError, match="unknown review agent profile"):
        service.prepare_review_route("not_a_profile")


def test_service_records_content_free_task_outcome(tmp_path):
    service = RouterService.from_settings(data_dir=tmp_path)

    receipt = service.record_qa_task_outcome(
        task_type="ordinary_review",
        outcome="completed",
        codegraph_calls=1,
        source_mcp_calls=2,
        findings_identified=3,
        findings_confirmed=2,
        findings_rejected=1,
        repeated_source_reads=1,
        deep_analysis_used=True,
        deep_model="gpt-5.6-sol",
        deep_reasoning="high",
        deep_duration_ms=1200,
    )

    assert receipt.status == "recorded"
    event = json.loads((tmp_path / "metrics.jsonl").read_text(encoding="utf-8"))
    assert event["event_type"] == "qa_task_outcome"
    assert event["deep_model"] == "gpt-5.6-sol"
    assert "legacy_metric" not in event


def test_service_rejects_inconsistent_task_outcome(tmp_path):
    service = RouterService.from_settings(data_dir=tmp_path)

    with pytest.raises(ValueError, match="QA task metrics are inconsistent"):
        service.record_qa_task_outcome(
            task_type="ordinary_review",
            outcome="completed",
            codegraph_calls=0,
            source_mcp_calls=0,
            findings_identified=1,
            findings_confirmed=2,
            findings_rejected=0,
            repeated_source_reads=0,
        )
