import hashlib
import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from qa_orchestrator.contracts import ReviewContext
from qa_orchestrator.evidence import evidence_main, input_manifest_completeness
from qa_orchestrator.orchestration import AdvanceQaOrchestrationRequest
from qa_orchestrator.server import build_server
from qa_orchestrator.service import OrchestratorService


def context_with_sources(base, sources):
    data = base.model_dump(mode="json")
    data["input_manifest"] = {
        "schema_version": 2,
        "expected_source_kinds": [],
        "source_inventory_status": "complete",
        "source_conflict_status": "clear",
        "sources": sources,
    }
    return ReviewContext.model_validate(data)


def source(source_id, kind, **overrides):
    return {
        "source_id": source_id, "kind": kind, "locator": f"host:{source_id}",
        "required": True, "retrieval_status": "complete",
        "pagination_status": "not_applicable", "extraction_status": "complete",
        **overrides,
    }


def test_available_source_without_criteria_assessment_is_rejected_with_recovery(tmp_path, bound_review_context):
    context = context_with_sources(bound_review_context, [source("MR", "merge_request")])
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    with pytest.raises(ValueError, match="MR.*criteria.*rebuild.*start"):
        service.start_qa_orchestration("ordinary_review", context, require_review_preflight=True)
    assert service.list_qa_orchestrations().sessions == []


def test_explicit_empty_assessment_allows_start(tmp_path, bound_review_context):
    context = context_with_sources(bound_review_context, [source("MR", "merge_request", criteria_status="none")])
    session = OrchestratorService.from_settings(data_dir=tmp_path).start_qa_orchestration(
        "ordinary_review", context, require_review_preflight=True
    )
    assert session.status == "active"
    assert input_manifest_completeness(context)["status"] == "complete"


def test_unavailable_source_may_start_but_remains_partial(tmp_path, bound_review_context):
    context = context_with_sources(bound_review_context, [source(
        "JIRA", "task", retrieval_status="failed", extraction_status="unknown"
    )])
    session = OrchestratorService.from_settings(data_dir=tmp_path).start_qa_orchestration(
        "ordinary_review", context, require_review_preflight=True
    )
    assert session.status == "active"
    assert input_manifest_completeness(context)["status"] == "partial"


def test_recorded_criteria_require_references(bound_review_context):
    with pytest.raises(ValueError, match="criteria_status"):
        context_with_sources(bound_review_context, [source("MR", "merge_request", criteria_status="recorded")])


def test_none_assessment_cannot_hide_recorded_criteria(bound_review_context):
    with pytest.raises(ValueError, match="criteria_status"):
        context_with_sources(bound_review_context, [source("MR", "merge_request", criteria_status="none", criterion_refs=["E1"])])


def test_unavailable_source_cannot_attest_no_criteria(bound_review_context):
    with pytest.raises(ValueError, match="criteria_status none.*complete"):
        context_with_sources(bound_review_context, [source("JIRA", "task", criteria_status="none", retrieval_status="failed")])


def test_expected_task_cannot_be_silently_omitted(tmp_path, bound_review_context):
    data = context_with_sources(bound_review_context, [source("MR", "merge_request", criteria_status="none")]).model_dump()
    data["input_manifest"]["expected_source_kinds"] = ["task", "merge_request"]
    context = ReviewContext.model_validate(data)
    with pytest.raises(ValueError, match="task.*source"):
        OrchestratorService.from_settings(data_dir=tmp_path).start_qa_orchestration(
            "ordinary_review", context, require_review_preflight=True
        )


def test_host_must_declare_expected_input_kinds_even_if_empty(tmp_path, bound_review_context):
    data = context_with_sources(bound_review_context, [source("MR", "merge_request", criteria_status="none")]).model_dump()
    data["input_manifest"].pop("expected_source_kinds", None)
    with pytest.raises(ValueError, match="expected_source_kinds"):
        OrchestratorService.from_settings(data_dir=tmp_path).start_qa_orchestration(
            "ordinary_review", ReviewContext.model_validate(data), require_review_preflight=True
        )


def test_fixing_recoverable_preparation_proceeds_to_triage(tmp_path, bound_review_context):
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    incomplete = context_with_sources(bound_review_context, [source("JIRA", "task"), source("MR", "merge_request")])
    with pytest.raises(ValueError, match="criteria"):
        service.start_qa_orchestration("ordinary_review", incomplete, require_review_preflight=True)
    corrected = context_with_sources(bound_review_context, [
        source("JIRA", "task", criteria_status="none"),
        source("MR", "merge_request", criteria_status="none"),
    ])
    started = service.start_qa_orchestration("ordinary_review", corrected, require_review_preflight=True)
    assert started.current_step == "triage"


def test_recorded_criterion_must_have_registered_evidence(tmp_path, bound_review_context):
    context = context_with_sources(bound_review_context, [
        source("JIRA", "task", criteria_status="recorded", criterion_refs=["REQ1"]),
    ])
    with pytest.raises(ValueError, match="REQ1.*register"):
        OrchestratorService.from_settings(data_dir=tmp_path).start_qa_orchestration(
            "ordinary_review", context, require_review_preflight=True
        )


def test_early_triage_stop_includes_recovery_advice(tmp_path, bound_review_context):
    context = context_with_sources(bound_review_context, [source("JIRA", "task", retrieval_status="failed")])
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    started = service.start_qa_orchestration("ordinary_review", context, require_review_preflight=True)
    stopped = service.advance_qa_orchestration(AdvanceQaOrchestrationRequest(
        run_id=started.run_id, completed_step="triage", status="partial",
        stop_reason="Source unavailable", preparation_recovery="unavailable",
    ))
    assert "recover" in stopped.route_warning.lower()
    assert "unavailable" in stopped.route_warning.lower()


def test_recoverable_triage_stop_requires_recovery_assessment(tmp_path, bound_review_context):
    context = context_with_sources(bound_review_context, [source("JIRA", "task", retrieval_status="failed")])
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    started = service.start_qa_orchestration("ordinary_review", context, require_review_preflight=True)
    request = AdvanceQaOrchestrationRequest(
        run_id=started.run_id, completed_step="triage", status="partial", stop_reason="Incomplete packet",
    )
    with pytest.raises(ValueError, match="preparation_recovery"):
        service.advance_qa_orchestration(request)
    assert service.get_qa_orchestration(started.run_id).status == "active"
    with pytest.raises(ValueError, match="[Rr]ecover"):
        service.advance_qa_orchestration(request.model_copy(update={"preparation_recovery": "not_attempted"}))
    assert service.get_qa_orchestration(started.run_id).status == "active"
    stopped = service.advance_qa_orchestration(request.model_copy(update={"preparation_recovery": "attempted"}))
    assert stopped.preparation_recovery == "attempted"


def test_task_and_mr_criteria_are_bound_to_correct_evidence(tmp_path, bound_review_context):
    data = bound_review_context.model_dump(mode="json")
    data["input_manifest"] = {
        "schema_version": 2, "source_inventory_status": "complete", "source_conflict_status": "clear",
        "expected_source_kinds": ["task", "merge_request"],
        "sources": [
            source("JIRA", "task", criteria_status="recorded", criterion_refs=["E1"]),
            source("MR", "merge_request", criteria_status="recorded", criterion_refs=["E2"]),
        ],
    }
    data["evidence_registry"] = [
        {"evidence_ref": "E1", "kind": "requirement", "locator": "host:jira", "sha256": "a" * 64},
        {"evidence_ref": "E2", "kind": "claim", "locator": "host:mr", "sha256": "b" * 64},
    ]
    context = ReviewContext.model_validate(data)
    service = OrchestratorService.from_settings(data_dir=tmp_path)
    assert service.start_qa_orchestration("ordinary_review", context, require_review_preflight=True).current_step == "triage"
    data["evidence_registry"][0]["kind"] = "claim"
    with pytest.raises(ValueError, match="E1.*requirement"):
        service.start_qa_orchestration("ordinary_review", ReviewContext.model_validate(data), require_review_preflight=True)


@pytest.mark.asyncio
async def test_mcp_preflight_recovery_with_rebuilt_diff_packet(tmp_path, bound_review_context):
    diff = b"diff --git a/src/service.py b/src/service.py\n--- a/src/service.py\n+++ b/src/service.py\n@@ -1 +1 @@\n-old\n+new\n"
    digest = lambda value: hashlib.sha256(value).hexdigest()
    requirement, claim = "Expected order", "Changes order"
    data = bound_review_context.model_dump(mode="json")
    data.update(input_kind="diff", base_sha=None, head_sha=None, diff_sha256=digest(diff),
                diff_hunks=[], diff_hunk_inventory_sha256=None, evidence_refs=["D1", "R1", "C1"])
    data["evidence_registry"] = [
        {"evidence_ref": "D1", "kind": "diff", "sha256": digest(diff)},
        {"evidence_ref": "R1", "kind": "requirement", "locator": "host:jira", "sha256": digest(requirement.encode())},
        {"evidence_ref": "C1", "kind": "claim", "locator": "host:mr", "sha256": digest(claim.encode())},
    ]
    data["input_manifest"] = {
        "schema_version": 2, "source_inventory_status": "complete", "source_conflict_status": "clear",
        "expected_source_kinds": ["task", "merge_request"],
        "sources": [source("JIRA", "task"), source("MR", "merge_request")],
    }
    diff_path, context_path, content_path = (tmp_path / name for name in ("change.diff", "context.json", "content.json"))
    diff_path.write_bytes(diff)
    content_path.write_text(json.dumps({"R1": requirement, "C1": claim}))

    def pack(suffix):
        context_path.write_text(json.dumps(data))
        bound_path = tmp_path / f"bound-{suffix}.json"
        assert evidence_main(["pack-diff", "--context", str(context_path), "--diff", str(diff_path),
                              "--content", str(content_path), "--packet", str(tmp_path / f"packet-{suffix}.json"),
                              "--bound-context", str(bound_path)]) == 0
        return json.loads(bound_path.read_text())

    service = OrchestratorService.from_settings(data_dir=tmp_path)
    async with Client(build_server(service)) as client:
        with pytest.raises(ToolError, match="criteria assessment"):
            await client.call_tool("start_qa_orchestration", {"task_type": "ordinary_review", "review_context": pack("incomplete")})
        assert not service.list_qa_orchestrations().sessions
        data["input_manifest"]["sources"][0].update(criteria_status="recorded", criterion_refs=["R1"])
        data["input_manifest"]["sources"][1].update(criteria_status="recorded", criterion_refs=["C1"])
        started = await client.call_tool("start_qa_orchestration", {"task_type": "ordinary_review", "review_context": pack("corrected")})
        assert started.structured_content["current_step"] == "triage"
        stop = {"run_id": started.structured_content["run_id"], "completed_step": "triage", "status": "partial", "stop_reason": "No reviewer available"}
        with pytest.raises(ToolError, match="preparation_recovery"):
            await client.call_tool("advance_qa_orchestration", stop)
        stop.update(preparation_recovery="unavailable", request_id="stop-1", expected_revision=0)
        result = await client.call_tool("advance_qa_orchestration", stop)
        assert result.structured_content["preparation_recovery"] == "unavailable"
        replay = await client.call_tool("advance_qa_orchestration", stop)
        assert replay.structured_content["revision"] == result.structured_content["revision"]
        finished = await client.call_tool("finish_qa_orchestration", {"run_id": stop["run_id"], "outcome": "partial"})
        assert finished.structured_content["preparation_recovery"] == "unavailable"
