import json
from concurrent.futures import ThreadPoolExecutor
from fcntl import LOCK_EX, LOCK_UN, flock

import pytest

from qa_router_mcp.backends import BackendError
from qa_router_mcp.config import Settings
from qa_router_mcp.contracts import DraftEnvelope, DraftKind, ReviewAgent
from qa_router_mcp.events import CANARY_TARGET, CANARY_TOOL_TARGETS, JsonEventSink
from qa_router_mcp.service import RouterService


class DraftFake:
    def __init__(self, result, repaired=None, token_count=100):
        self.results = [result] if repaired is None else [result, repaired]
        self.prompts = []
        self.token_prompts = []
        self.output_limits = []
        self.token_count = token_count

    async def count_tokens(self, prompt):
        self.token_prompts.append(prompt)
        return self.token_count

    async def generate(self, prompt, *, max_output_tokens=None, allow_schema_repair=True):
        self.prompts.append(prompt)
        self.output_limits.append(max_output_tokens)
        return self.results.pop(0)


@pytest.mark.asyncio
async def test_unknown_review_profile_fails_before_backend(tmp_path):
    drafting = DraftFake(DraftEnvelope(draft="unused", unverified=[]))
    service = RouterService(Settings(data_dir=tmp_path), drafting)

    with pytest.raises(ValueError, match="unknown review agent profile"):
        await service.draft(
            DraftKind.REVIEW_CHECKLIST,
            "changed checkout behavior",
            review_agent="not_registered",
        )

    assert drafting.token_prompts == []
    assert drafting.prompts == []


@pytest.mark.asyncio
async def test_review_lane_reuses_quality_metadata_and_profile_prompt(tmp_path, monkeypatch):
    review = DraftEnvelope(
        draft=(
            "Scope: changed checkout behavior\nChecklist: inspect branches\n"
            "Candidate Coverage Gaps: error path\nPositive Observations: bounded diff\n"
            "Unverified: runtime behavior"
        ),
        unverified=["Review against source"],
    )
    drafting = DraftFake(review)
    service = RouterService(Settings(data_dir=tmp_path), drafting)
    monkeypatch.setattr("qa_router_mcp.service.is_shadow_sample", lambda _: False)

    result = await service.draft(
        DraftKind.REVIEW_CHECKLIST,
        "changed checkout behavior",
        review_agent=ReviewAgent.PR_TEST_ANALYZER,
    )

    assert result.status == "ok"
    assert result.quality_status == "canary"
    assert result.canary_feedback_required is True
    assert result.draft_id is not None
    assert "REVIEW_AGENT_PROFILE: pr_test_analyzer" in drafting.prompts[0]
    assert '"tool":"review_checklist"' in (tmp_path / "metrics.jsonl").read_text()


@pytest.mark.asyncio
async def test_draft_sanitizes_transient_identifiers(tmp_path):
    drafting = DraftFake(
        DraftEnvelope(
            draft=(
                "Title: Guest checkout\nPreconditions: Guest user\n"
                "Steps: 1. Submit checkout\nExpected Result: Checkout is submitted"
            ),
            unverified=["Expected result"],
        )
    )
    service = RouterService(Settings(data_dir=tmp_path), drafting)

    result = await service.draft(
        DraftKind.TEST_CASES,
        "ABC-123 at https://stage.test",
    )

    assert result.status == "ok"
    assert "ABC-123" not in drafting.prompts[0]
    assert "[ISSUE]" in drafting.prompts[0]


@pytest.mark.asyncio
async def test_examples_do_not_become_required_coverage_ids(tmp_path):
    class StableDraft:
        async def count_tokens(self, prompt):
            return 100

        async def generate(self, prompt, *, max_output_tokens=None, allow_schema_repair=True):
            return DraftEnvelope(
                draft=(
                    "Coverage ID: COV-REAL\nTitle: Case\nPreconditions: Ready\n"
                    "Steps: 1. Act\nExpected Result: Success"
                ),
                unverified=[],
            )

    service = RouterService(Settings(data_dir=tmp_path), StableDraft())

    result = await service.draft(
        DraftKind.TEST_CASES,
        "Draft exactly 1 test case\nAPPROVED_COVERAGE_MAP:\nCoverage ID: COV-REAL\n"
        "Purpose: checkout\nEXAMPLES:\nCoverage ID: COV-EXAMPLE",
        expected_coverage_ids=("COV-REAL",),
    )

    assert result.status == "ok"


def test_quality_gate_waits_for_shared_metrics_lock(tmp_path):
    path = tmp_path / "metrics.jsonl"
    path.write_text('{"timestamp":"2026-01-01T00:00:00+00:00"}\n')
    sink = JsonEventSink(path)
    with path.open("a+") as metrics:
        flock(metrics.fileno(), LOCK_EX)
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(sink.quality_gate, "translation", "router-v11")
                assert not future.done()
                flock(metrics.fileno(), LOCK_UN)
                future.result(timeout=1)
        finally:
            try:
                flock(metrics.fileno(), LOCK_UN)
            except OSError:
                pass


def test_quality_gate_fails_closed_when_metrics_are_corrupt(tmp_path):
    path = tmp_path / "metrics.jsonl"
    path.write_text("not-json\n")

    gate = JsonEventSink(path).quality_gate("translation", "router-v11")

    assert gate.status == "paused"


def test_quality_gate_fails_closed_when_metrics_cannot_be_read(tmp_path, monkeypatch):
    sink = JsonEventSink(tmp_path / "metrics.jsonl")

    def unavailable(_):
        raise OSError("read failed")

    monkeypatch.setattr("qa_router_mcp.events.read_metrics_lines", unavailable)

    assert sink.quality_gate("translation", "router-v11").status == "paused"


@pytest.mark.asyncio
async def test_policy_failure_returns_refusal_without_backend_call(tmp_path):
    drafting = DraftFake(DraftEnvelope(draft="unused", unverified=["unused"]))
    service = RouterService(Settings(data_dir=tmp_path), drafting)

    result = await service.draft(DraftKind.TEST_CASES, "password=secret")

    assert result.status == "refused"
    assert result.reason == "secret_detected"
    assert result.sensitive_category == "possible_secret"
    assert drafting.prompts == []
    assert drafting.token_prompts == []
    event = json.loads((tmp_path / "metrics.jsonl").read_text())
    assert event["token_usage_available"] is False
    assert event["phase_latency_available"] is False


@pytest.mark.parametrize(
    "input_text",
    [
        "Draft 13 test cases",
        "Draft thirteen test cases",
        "Draft twenty test cases",
        "Составь тринадцать кейсов",
        "Составь двадцать кейсов",
    ],
)
@pytest.mark.asyncio
async def test_more_than_twelve_test_cases_are_refused_before_backend_call(tmp_path, input_text):
    drafting = DraftFake(DraftEnvelope(draft="unused", unverified=["unused"]))
    service = RouterService(Settings(data_dir=tmp_path), drafting)

    result = await service.draft(DraftKind.TEST_CASES, input_text)

    assert result.status == "refused"
    assert result.reason == "requested_case_count_too_large"
    assert drafting.token_prompts == []
    assert drafting.prompts == []


@pytest.mark.asyncio
async def test_combined_packet_limit_is_enforced_before_backend(tmp_path):
    drafting = DraftFake(DraftEnvelope(draft="unused", unverified=["unused"]))
    settings = Settings(data_dir=tmp_path, max_input_chars=10)
    service = RouterService(settings, drafting)

    result = await service.draft(DraftKind.AUTOMATION_SKELETON, "123456", "78901")

    assert result.status == "refused"
    assert result.reason == "input_too_large"
    assert drafting.prompts == []


@pytest.mark.asyncio
async def test_token_budget_is_enforced_before_backend(tmp_path):
    drafting = DraftFake(
        DraftEnvelope(draft="unused", unverified=["unused"]),
        token_count=14_992,
    )
    service = RouterService(
        Settings(data_dir=tmp_path),
        drafting,
        JsonEventSink(tmp_path / "metrics.jsonl"),
    )

    result = await service.draft(DraftKind.TEST_CASES, "A1b2" * 5_000)

    assert result.status == "refused"
    assert result.reason == "token_budget_exceeded"
    assert drafting.prompts == []

    event = (tmp_path / "metrics.jsonl").read_text()
    assert '"estimated_prompt_tokens"' in event
    assert '"context_tokens":16384' in event


@pytest.mark.asyncio
async def test_tokenizer_failure_falls_back_before_generation(tmp_path):
    class FailingTokenizerBackend(DraftFake):
        async def count_tokens(self, prompt):
            raise BackendError("local_tokenizer_error")

    drafting = FailingTokenizerBackend(DraftEnvelope(draft="unused", unverified=["unused"]))
    service = RouterService(Settings(data_dir=tmp_path), drafting)

    result = await service.draft(DraftKind.LOG_SUMMARY, "Synthetic timeout")

    assert result.status == "fallback"
    assert result.reason == "local_tokenizer_error"
    assert drafting.prompts == []


@pytest.mark.asyncio
async def test_new_active_profile_requests_feedback_until_minimum_reviews(tmp_path, monkeypatch):
    drafting = DraftFake(DraftEnvelope(draft="Translated", unverified=[]))
    service = RouterService(Settings(data_dir=tmp_path), drafting)
    monkeypatch.setattr("qa_router_mcp.service.is_shadow_sample", lambda _: False)

    result = await service.draft(DraftKind.TRANSLATION, "Translate: hello")

    assert result.status == "ok"
    assert result.quality_status == "active"
    assert result.canary_feedback_required is True
    assert result.draft_id is not None
    event = (tmp_path / "metrics.jsonl").read_text()
    assert '"source":"interactive"' in event
    assert '"quality_status":"active"' in event


def test_qa_task_event_cannot_override_envelope_fields(tmp_path):
    metrics_path = tmp_path / "metrics.jsonl"
    sink = JsonEventSink(metrics_path)

    receipt = sink.record_qa_task_outcome(
        {
            "schema_version": 1,
            "event_type": "generation",
            "timestamp": "2000-01-01T00:00:00+00:00",
            "task_type": "other",
            "outcome": "completed",
        }
    )

    event = json.loads(metrics_path.read_text())
    assert receipt.status == "recorded"
    assert event["schema_version"] == 7
    assert event["event_type"] == "qa_task_outcome"
    assert event["timestamp"] != "2000-01-01T00:00:00+00:00"


@pytest.mark.asyncio
async def test_benchmark_draft_does_not_request_canary_feedback(tmp_path):
    drafting = DraftFake(DraftEnvelope(draft="Translated", unverified=[]))
    settings = Settings(data_dir=tmp_path, metrics_source="benchmark")
    service = RouterService(settings, drafting)

    result = await service.draft(DraftKind.TRANSLATION, "Translate: hello")

    assert result.status == "ok"
    assert result.canary_feedback_required is False
    assert result.draft_id is None


@pytest.mark.asyncio
async def test_paused_quality_gate_falls_back_before_tokenization(tmp_path):
    path = tmp_path / "metrics.jsonl"
    drafting = DraftFake(DraftEnvelope(draft="unused", unverified=[]))
    service = RouterService(Settings(data_dir=tmp_path), drafting, JsonEventSink(path))
    events = []
    for index in range(10):
        draft_id = f"{index + 1:032x}"
        events.extend(
            [
                {
                    "schema_version": 4,
                    "timestamp": "2026-09-03T00:00:00+00:00",
                    "tool": "test_cases",
                    "model": "qwen/qwen3.5-9b",
                    "profile_version": Settings().profile_version,
                    "source": "interactive",
                    "outcome": "ok",
                    "draft_id": draft_id,
                },
                {
                    "schema_version": 4,
                    "event_type": "canary_feedback",
                    "timestamp": "2026-09-03T00:00:01+00:00",
                    "tool": "test_cases",
                    "draft_id": draft_id,
                    "profile_version": Settings().profile_version,
                    "source": "interactive",
                    "verdict": "edited" if index < 2 else "accepted",
                    "reason": "coverage" if index < 2 else "none",
                },
            ]
        )
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n")

    result = await service.draft(DraftKind.TEST_CASES, "Draft one test case")

    assert result.status == "fallback"
    assert result.reason == "quality_gate_paused"
    assert result.quality_status == "paused"
    assert drafting.token_prompts == []
    assert drafting.prompts == []


@pytest.mark.asyncio
async def test_generation_event_records_phase_timings_and_cold_start_hint(tmp_path):
    path = tmp_path / "metrics.jsonl"
    drafting = DraftFake(
        DraftEnvelope(draft="Translated", unverified=[]),
        DraftEnvelope(draft="Translated", unverified=[]),
    )
    service = RouterService(Settings(data_dir=tmp_path), drafting, JsonEventSink(path))

    first = await service.draft(DraftKind.TRANSLATION, "Translate: hello")
    second = await service.draft(DraftKind.TRANSLATION, "Translate: goodbye")

    events = [json.loads(line) for line in path.read_text().splitlines()]
    assert first.cold_start_likely is True
    assert second.cold_start_likely is False
    assert all(event["tokenization_ms"] >= 0 for event in events)
    assert all(event["model_load_ms"] >= 0 for event in events)
    assert all(event["generation_ms"] >= 0 for event in events)
    assert all(event["validation_ms"] >= 0 for event in events)
    assert all(event["repair_ms"] >= 0 for event in events)
    assert all(event["duration_ms"] >= event["generation_ms"] for event in events)
    assert all(event["phase_latency_available"] is True for event in events)


@pytest.mark.asyncio
async def test_active_route_requests_feedback_only_for_shadow_sample(tmp_path, monkeypatch):
    drafting = DraftFake(DraftEnvelope(draft="Translated", unverified=[]))
    service = RouterService(Settings(data_dir=tmp_path), drafting)
    monkeypatch.setattr("qa_router_mcp.service.is_shadow_sample", lambda _: True)

    result = await service.draft(DraftKind.TRANSLATION, "Translate: hello")

    assert result.quality_status == "active"
    assert result.shadow_evaluation_required is True
    assert result.canary_feedback_required is True
    assert result.draft_id is not None


@pytest.mark.asyncio
async def test_short_explanation_shadow_sample_can_record_feedback(tmp_path, monkeypatch):
    drafting = DraftFake(DraftEnvelope(draft="Retries repeat a failed operation.", unverified=[]))
    service = RouterService(Settings(data_dir=tmp_path), drafting)
    monkeypatch.setattr("qa_router_mcp.service.is_shadow_sample", lambda _: True)

    result = await service.draft(DraftKind.SHORT_EXPLANATION, "Explain retries")
    receipt = service.record_canary_feedback(result.draft_id, "accepted", "none")

    assert result.shadow_evaluation_required is True
    assert result.draft_id is not None
    assert receipt.status == "recorded"


@pytest.mark.asyncio
async def test_structured_qa_draft_may_have_no_unverified_claims(tmp_path):
    complete = DraftEnvelope(
        draft=(
            "Title: Case\nPreconditions: Ready\nSteps: 1. Act\nExpected Result: Expected behavior"
        ),
        unverified=[],
    )
    drafting = DraftFake(complete)
    service = RouterService(Settings(data_dir=tmp_path), drafting)

    result = await service.draft(DraftKind.TEST_CASES, "Guest checkout")

    assert result.status == "ok"
    assert result.unverified == []
    assert len(drafting.prompts) == 1


@pytest.mark.asyncio
async def test_malformed_test_cases_are_repaired_once(tmp_path):
    malformed = DraftEnvelope(draft="Title: Only one case", unverified=["Review"])
    repaired = DraftEnvelope(
        draft=(
            "Title: Case 1\nPreconditions: Ready\nSteps: 1. Act\nExpected Result: One\n\n"
            "Title: Case 2\nPreconditions: Ready\nSteps: 1. Act\nExpected Result: Two\n\n"
            "Title: Case 3\nPreconditions: Ready\nSteps: 1. Act\nExpected Result: Three"
        ),
        unverified=["Review"],
    )
    drafting = DraftFake(malformed, repaired)
    service = RouterService(Settings(data_dir=tmp_path), drafting)

    result = await service.draft(DraftKind.TEST_CASES, "Draft 3 test cases")

    assert result.status == "ok"
    assert len(drafting.prompts) == 2
    assert "REPAIR_REQUIRED" in drafting.prompts[1]


@pytest.mark.asyncio
async def test_semantic_repair_rechecks_context_budget(tmp_path):
    malformed = DraftEnvelope(draft="Title: Only one case", unverified=["Review"])

    class GrowingPromptBackend(DraftFake):
        async def count_tokens(self, prompt):
            self.token_prompts.append(prompt)
            return 100 if len(self.token_prompts) == 1 else 15_000

    drafting = GrowingPromptBackend(malformed, malformed)
    path = tmp_path / "metrics.jsonl"
    service = RouterService(Settings(data_dir=tmp_path), drafting, JsonEventSink(path))

    result = await service.draft(DraftKind.TEST_CASES, "Draft 3 test cases")

    assert result.status == "refused"
    assert result.reason == "token_budget_exceeded"
    assert len(drafting.token_prompts) == 2
    assert len(drafting.prompts) == 1
    event = json.loads(path.read_text())
    assert event["validation_repair"] is True


@pytest.mark.asyncio
async def test_malformed_test_cases_fall_back_after_one_repair(tmp_path):
    malformed = DraftEnvelope(draft="Title: Only one case", unverified=["Review"])
    drafting = DraftFake(malformed, malformed)
    service = RouterService(Settings(data_dir=tmp_path), drafting)

    result = await service.draft(DraftKind.TEST_CASES, "Draft 3 test cases")

    assert result.status == "fallback"
    assert result.reason == "local_model_invalid_draft"
    assert len(drafting.prompts) == 2


@pytest.mark.asyncio
async def test_failed_semantic_repair_is_counted_in_metrics(tmp_path):
    malformed = DraftEnvelope(draft="Title: Only one case", unverified=["Review"])

    class FailingRepairBackend(DraftFake):
        async def generate(self, prompt, *, max_output_tokens=None, allow_schema_repair=True):
            self.prompts.append(prompt)
            if len(self.prompts) == 2:
                raise BackendError("local_model_invalid_response")
            return malformed

    path = tmp_path / "metrics.jsonl"
    service = RouterService(
        Settings(data_dir=tmp_path),
        FailingRepairBackend(malformed),
        JsonEventSink(path),
    )

    result = await service.draft(DraftKind.TEST_CASES, "Draft 3 test cases")

    assert result.status == "fallback"
    assert '"validation_repair":true' in path.read_text()


@pytest.mark.asyncio
async def test_truncated_draft_falls_back_without_repair(tmp_path):
    from qa_router_mcp.contracts import GenerationStats

    truncated = DraftEnvelope(draft="Translated", unverified=[])
    truncated.set_generation_stats(GenerationStats(requests=1, truncated=True))
    drafting = DraftFake(truncated)
    service = RouterService(Settings(data_dir=tmp_path), drafting)

    result = await service.draft(DraftKind.TRANSLATION, "Translate this")

    assert result.status == "fallback"
    assert result.reason == "local_model_truncated"
    assert len(drafting.prompts) == 1


@pytest.mark.asyncio
async def test_service_applies_per_tool_output_budget(tmp_path):
    drafting = DraftFake(DraftEnvelope(draft="Short explanation", unverified=[]))
    service = RouterService(Settings(data_dir=tmp_path), drafting)

    result = await service.draft(DraftKind.SHORT_EXPLANATION, "Explain retries")

    assert result.status == "ok"
    assert drafting.output_limits == [512]


@pytest.mark.asyncio
async def test_routine_text_draft_without_unverified_succeeds(tmp_path):
    routine = DraftEnvelope(draft="Translated text", unverified=[])
    drafting = DraftFake(routine)
    service = RouterService(Settings(data_dir=tmp_path), drafting)

    result = await service.draft(DraftKind.TRANSLATION, "Translate this")

    assert result.status == "ok"
    assert result.unverified == []


def test_event_sink_never_logs_content(capsys):
    JsonEventSink().emit("draft_test_cases", "refused", 1.25, "secret_detected")

    event = capsys.readouterr().err
    assert "draft_test_cases" in event
    assert "secret_detected" in event
    assert "password=secret" not in event


def test_event_sink_logs_usage_without_content(capsys, tmp_path):
    from qa_router_mcp.contracts import GenerationStats

    path = tmp_path / "metrics.jsonl"
    JsonEventSink(path).emit(
        "translate_text",
        "ok",
        2.5,
        None,
        input_chars=50,
        stats=GenerationStats(prompt_tokens=20, output_tokens=10, requests=1),
        model="qwen/qwen3.5-9b",
        profile_version="router-v2",
        source="benchmark",
        estimated_prompt_tokens=42,
        context_tokens=16_384,
    )

    event = __import__("json").loads(path.read_text())
    assert event["schema_version"] == 7
    assert event["model"] == "qwen/qwen3.5-9b"
    assert event["profile_version"] == "router-v2"
    assert event["source"] == "benchmark"
    assert event["estimated_prompt_tokens"] == 42
    assert event["context_tokens"] == 16_384
    assert event["prompt_tokens"] == 20
    assert event["output_tokens"] == 10
    assert event["token_usage_available"] is True
    assert event["input_chars"] == 50
    assert "Translated text" not in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600


def test_service_records_content_free_qa_task_outcome(tmp_path):
    path = tmp_path / "metrics.jsonl"
    service = RouterService(
        Settings(data_dir=tmp_path),
        DraftFake(DraftEnvelope(draft="unused", unverified=[])),
        JsonEventSink(path),
    )

    receipt = service.record_qa_task_outcome(
        task_type="ordinary_review",
        outcome="completed",
        codegraph_calls=2,
        source_mcp_calls=7,
        qwen_used=True,
        sol_used=False,
        findings_identified=3,
        findings_confirmed=2,
        findings_rejected=1,
        qwen_edits=1,
        repeated_source_reads=0,
    )

    event = json.loads(path.read_text())
    assert receipt.status == "recorded"
    assert event == {
        "schema_version": 7,
        "event_type": "qa_task_outcome",
        "timestamp": event["timestamp"],
        "task_type": "ordinary_review",
        "outcome": "completed",
        "codegraph_calls": 2,
        "source_mcp_calls": 7,
        "qwen_used": True,
        "deep_analysis_used": False,
        "findings_identified": 3,
        "findings_confirmed": 2,
        "findings_rejected": 1,
        "qwen_edits": 1,
        "repeated_source_reads": 0,
    }
    serialized = path.read_text()
    assert "requirement" not in serialized
    assert "jira" not in serialized.lower()
    assert "draft" not in serialized


def test_codegraph_and_other_mcp_counters_are_independent(tmp_path):
    service = RouterService(
        Settings(data_dir=tmp_path),
        DraftFake(DraftEnvelope(draft="unused", unverified=[])),
    )

    receipt = service.record_qa_task_outcome(
        task_type="other",
        outcome="completed",
        codegraph_calls=1,
        source_mcp_calls=0,
        qwen_used=False,
        sol_used=False,
        findings_identified=0,
        findings_confirmed=0,
        findings_rejected=0,
        qwen_edits=0,
        repeated_source_reads=0,
    )

    assert receipt.status == "recorded"


def test_qa_task_outcome_is_unavailable_without_metrics_path():
    receipt = JsonEventSink().record_qa_task_outcome(
        {
            "task_type": "ordinary_review",
            "outcome": "completed",
            "codegraph_calls": 0,
            "source_mcp_calls": 0,
            "qwen_used": False,
            "sol_used": False,
            "findings_identified": 0,
            "findings_confirmed": 0,
            "findings_rejected": 0,
            "qwen_edits": 0,
            "repeated_source_reads": 0,
        }
    )

    assert receipt.status == "unavailable"


def test_metrics_retention_prunes_expired_and_excess_events(tmp_path):
    path = tmp_path / "metrics.jsonl"
    path.write_text(
        json.dumps(
            {
                "timestamp": "2020-01-01T00:00:00+00:00",
                "tool": "rewrite",
                "outcome": "ok",
            }
        )
        + "\n"
    )
    sink = JsonEventSink(path, retention_days=1, max_events=2)

    for _ in range(3):
        sink.emit("rewrite", "ok", 1, None)

    events = [json.loads(line) for line in path.read_text().splitlines()]

    assert len(events) == 2
    assert all(event["timestamp"] != "2020-01-01T00:00:00+00:00" for event in events)


@pytest.mark.parametrize(
    "overrides",
    [
        {"codegraph_calls": -1},
        {"findings_identified": 1, "findings_confirmed": 2},
        {"qwen_used": False, "qwen_edits": 1},
        {"task_type": "unknown"},
        {"outcome": "unknown"},
    ],
)
def test_qa_task_outcome_rejects_inconsistent_counters(tmp_path, overrides):
    service = RouterService(
        Settings(data_dir=tmp_path),
        DraftFake(DraftEnvelope(draft="unused", unverified=[])),
    )
    values = {
        "task_type": "ordinary_review",
        "outcome": "completed",
        "codegraph_calls": 1,
        "source_mcp_calls": 3,
        "qwen_used": True,
        "sol_used": False,
        "findings_identified": 2,
        "findings_confirmed": 1,
        "findings_rejected": 1,
        "qwen_edits": 0,
        "repeated_source_reads": 0,
    }
    values.update(overrides)

    with pytest.raises(ValueError, match="QA task metrics"):
        service.record_qa_task_outcome(**values)


def test_event_sink_records_content_free_canary_feedback(tmp_path):
    path = tmp_path / "metrics.jsonl"
    sink = JsonEventSink(path)
    draft_id = "a" * 32
    sink.emit(
        "test_cases",
        "ok",
        1.0,
        None,
        profile_version="router-v7",
        source="interactive",
        draft_id=draft_id,
    )

    receipt = sink.record_feedback(
        draft_id,
        "edited",
        "coverage",
    )

    events = [__import__("json").loads(line) for line in path.read_text().splitlines()]
    event = events[-1]
    assert receipt.status == "recorded"
    assert receipt.feedback_count == 1
    assert receipt.target == CANARY_TARGET
    assert event == {
        "schema_version": 4,
        "event_type": "canary_feedback",
        "timestamp": event["timestamp"],
        "tool": "test_cases",
        "draft_id": draft_id,
        "profile_version": "router-v7",
        "source": "interactive",
        "verdict": "edited",
        "reason": "coverage",
    }
    assert "draft" not in event
    assert "content" not in event


def test_canary_uses_per_profile_tool_quotas_across_sink_instances(tmp_path):
    path = tmp_path / "metrics.jsonl"
    sinks = [JsonEventSink(path) for _ in range(3)]
    draft_ids = [f"{index:032x}" for index in range(1, CANARY_TOOL_TARGETS["translation"] + 2)]
    issued_ids = []
    for index, draft_id in enumerate(draft_ids):
        sink = sinks[index % len(sinks)]
        issued_ids.append(
            sink.emit(
                "translation",
                "ok",
                1.0,
                None,
                profile_version="router-v7",
                source="interactive",
                draft_id=draft_id,
            )
        )

    assert issued_ids == draft_ids
    first = sinks[0].record_feedback(draft_ids[0], "accepted", "none")
    second = sinks[1].record_feedback(draft_ids[1], "accepted", "none")

    assert first.status == "recorded"
    assert second.status == "recorded"
    next_profile_draft_id = sinks[2].emit(
        "translation",
        "ok",
        1.0,
        None,
        profile_version="router-v8",
        source="interactive",
        draft_id="4" * 32,
    )
    assert next_profile_draft_id == "4" * 32
    next_profile_feedback = sinks[2].record_feedback("4" * 32, "accepted", "none")
    assert next_profile_feedback.feedback_count == 1


def test_canary_accepts_only_one_feedback_for_an_issued_draft(tmp_path):
    path = tmp_path / "metrics.jsonl"
    sink = JsonEventSink(path)
    draft_id = "a" * 32
    sink.emit(
        "test_cases",
        "ok",
        1.0,
        None,
        profile_version="router-v7",
        source="interactive",
        draft_id=draft_id,
    )

    recorded = sink.record_feedback(draft_id, "accepted", "none")
    duplicate = sink.record_feedback(draft_id, "accepted", "none")
    unknown = sink.record_feedback("f" * 32, "accepted", "none")

    assert recorded.status == "recorded"
    assert duplicate.status == "duplicate"
    assert unknown.status == "not_found"
    assert unknown.feedback_count == 1


def test_canary_completes_only_after_every_tool_quota(tmp_path):
    from qa_router_mcp.events import CANARY_TOOL_TARGETS

    path = tmp_path / "metrics.jsonl"
    sink = JsonEventSink(path)
    index = 0
    for tool, target in CANARY_TOOL_TARGETS.items():
        for _ in range(target):
            index += 1
            draft_id = f"{index:032x}"
            sink.emit(
                tool,
                "ok",
                1.0,
                None,
                profile_version="router-v7",
                source="interactive",
                draft_id=draft_id,
            )
            receipt = sink.record_feedback(draft_id, "accepted", "none")

    assert receipt.status == "recorded"
    assert receipt.feedback_count == CANARY_TARGET
    for tool in CANARY_TOOL_TARGETS:
        assert (
            sink.emit(
                tool,
                "ok",
                1.0,
                None,
                profile_version="router-v7",
                source="interactive",
                draft_id="f" * 32,
            )
            is None
        )


def test_shadow_feedback_is_recorded_after_canary_quota(tmp_path):
    from qa_router_mcp.events import CANARY_TOOL_TARGETS

    path = tmp_path / "metrics.jsonl"
    sink = JsonEventSink(path)
    for index in range(CANARY_TOOL_TARGETS["translation"]):
        draft_id = f"{index + 1:032x}"
        sink.emit(
            "translation",
            "ok",
            1.0,
            None,
            profile_version="router-v10",
            source="interactive",
            draft_id=draft_id,
        )
        sink.record_feedback(draft_id, "accepted", "none")

    shadow_id = "f" * 32
    issued = sink.emit(
        "translation",
        "ok",
        1.0,
        None,
        profile_version="router-v10",
        source="interactive",
        draft_id=shadow_id,
        quality_status="active",
        shadow_evaluation_required=True,
    )
    receipt = sink.record_feedback(shadow_id, "edited", "format")

    assert issued == shadow_id
    assert receipt.status == "recorded"
    assert receipt.feedback_count == CANARY_TOOL_TARGETS["translation"] + 1


def test_service_records_feedback_by_draft_id(tmp_path):
    service = RouterService(
        Settings(data_dir=tmp_path),
        DraftFake(DraftEnvelope(draft="unused", unverified=[])),
    )
    draft_id = "a" * 32
    service.events.emit(
        "test_cases",
        "ok",
        1.0,
        None,
        profile_version="router-v7",
        source="interactive",
        draft_id=draft_id,
    )

    receipt = service.record_canary_feedback(draft_id, "edited", "coverage")

    assert receipt.status == "recorded"


def test_malformed_feedback_does_not_consume_canary_quota(tmp_path):
    import json

    from qa_router_mcp.events import validated_canary_feedback

    path = tmp_path / "metrics.jsonl"
    sink = JsonEventSink(path)
    draft_id = "a" * 32
    sink.emit(
        "test_cases",
        "ok",
        1.0,
        None,
        profile_version="router-v7",
        source="interactive",
        draft_id=draft_id,
    )
    malformed = {
        "schema_version": 1,
        "event_type": "canary_feedback",
        "timestamp": "2026-09-03T00:00:00+00:00",
        "tool": "test_cases",
        "draft_id": draft_id,
        "profile_version": "router-v7",
        "source": "benchmark",
        "verdict": "bogus",
    }
    with path.open("a", encoding="utf-8") as metrics:
        metrics.write(json.dumps(malformed) + "\n")

    events = [json.loads(line) for line in path.read_text().splitlines()]

    assert validated_canary_feedback(events) == []
    assert (
        sink.emit(
            "test_cases",
            "ok",
            1.0,
            None,
            profile_version="router-v7",
            source="interactive",
            draft_id="b" * 32,
        )
        == "b" * 32
    )


@pytest.mark.parametrize(
    ("verdict", "reason"),
    [("accepted", "coverage"), ("edited", "none"), ("rejected", "none")],
)
def test_canary_feedback_requires_a_consistent_reason(tmp_path, verdict, reason):
    service = RouterService(
        Settings(data_dir=tmp_path),
        DraftFake(DraftEnvelope(draft="unused", unverified=[])),
    )

    with pytest.raises(ValueError, match="feedback reason"):
        service.record_canary_feedback("a" * 32, verdict, reason)
