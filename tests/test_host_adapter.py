import asyncio
from datetime import UTC, datetime

import pytest

from qa_orchestrator.contracts import (
    FinalReviewReport,
    ProfileExecutionMetrics,
    ProfileReviewReport,
    ReviewAgent,
)
from qa_orchestrator.host import HostExecutionPolicy, HostReviewAdapter, RetryableProfileError
from qa_orchestrator.orchestration import (
    AdvanceQaOrchestrationRequest,
    DeepReviewSignals,
    OrchestrationError,
)
from qa_orchestrator.service import OrchestratorService


@pytest.fixture
def host_session(tmp_path, bound_review_context):
    service = OrchestratorService.from_settings(data_dir=tmp_path)

    def start(*, bundle=None, profile=None):
        session = service.start_qa_orchestration("ordinary_review", bound_review_context)
        service.advance_qa_orchestration(
            AdvanceQaOrchestrationRequest(
                run_id=session.run_id,
                completed_step="triage",
                status="completed",
                selected_bundle=bundle,
                selected_profile=profile,
            )
        )
        return service, session.run_id

    return start


def report(profile):
    return ProfileReviewReport(profile=profile, scope="Pinned source inspected.")


async def test_finish_review_exports_markdown_with_the_accepted_outcome(host_session, tmp_path):
    service, run_id = host_session(profile="python_reviewer")
    adapter = HostReviewAdapter(service)

    async def execute(job):
        return report(job.profile)

    await adapter.run_primary_review(run_id, execute, lambda _: DeepReviewSignals())
    current = service.get_qa_orchestration(run_id)
    output = tmp_path / "review.md"
    output.write_text("previous artifact", encoding="utf-8")
    final_report = FinalReviewReport(
        summary="Review completed against the pinned context.",
        profiles_reviewed=(ReviewAgent.PYTHON_REVIEWER,),
    )
    with pytest.raises(OrchestrationError, match="outcome is not ready"):
        adapter.finish_review(
            run_id,
            outcome="completed",
            final_review_report=final_report,
            markdown_output=output,
        )
    assert output.read_text(encoding="utf-8") == "previous artifact"
    assert not list(tmp_path.glob(".review.md.*.tmp"))

    service.advance_qa_orchestration(
        AdvanceQaOrchestrationRequest(
            run_id=run_id,
            completed_step="synthesis",
            status="completed",
            expected_revision=current.revision,
        )
    )
    current = service.get_qa_orchestration(run_id)
    finished = adapter.finish_review(
        run_id,
        outcome="completed",
        final_review_report=final_report,
        expected_revision=current.revision,
        markdown_output=output,
    )

    assert finished.status == "completed"
    assert finished.final_review_report == final_report
    assert output.read_text(encoding="utf-8").startswith("# QA review\n")
    assert "Review completed against the pinned context" in output.read_text(encoding="utf-8")


async def test_attempt_ledger_preserves_failed_usage_and_sums_known_retries(host_session):
    service, run_id = host_session(profile="python_reviewer")

    async def execute(job):
        job.observation.input_tokens = 10 * job.attempt
        job.observation.output_tokens = 2 * job.attempt
        if job.attempt == 1:
            raise RetryableProfileError("secret")
        return report(job.profile)

    result = await HostReviewAdapter(
        service, HostExecutionPolicy(retry_delay_seconds=0)
    ).run_primary_review(run_id, execute, lambda _: DeepReviewSignals())
    assert [a.outcome for a in result.execution_attempts] == ["retryable_error", "completed"]
    metrics = result.session.profile_reports[0].execution_metrics
    assert metrics.input_tokens == 30
    assert metrics.output_tokens == 6
    assert metrics.attempts == result.execution_attempts
    assert "secret" not in str(result.execution_attempts)


async def test_cancelled_attempt_keeps_observed_usage_in_partial_result(host_session):
    service, run_id = host_session(profile="python_reviewer")

    async def execute(job):
        job.observation.input_tokens = 17
        await asyncio.Event().wait()

    result = await HostReviewAdapter(
        service, HostExecutionPolicy(run_timeout_seconds=0.02)
    ).run_primary_review(run_id, execute, lambda _: DeepReviewSignals())
    assert result.session.status == "partial"
    assert result.execution_attempts[0].outcome == "cancelled"
    assert result.execution_attempts[0].input_tokens == 17
    assert result.execution_attempts[0].output_tokens is None


async def test_attempt_ledger_reports_semaphore_wait(host_session):
    service, run_id = host_session(bundle="python_backend")

    async def execute(job):
        await asyncio.sleep(0.01)
        return report(job.profile)

    result = await HostReviewAdapter(
        service, HostExecutionPolicy(max_concurrency=1)
    ).run_primary_review(run_id, execute, lambda _: DeepReviewSignals())
    assert len(result.execution_attempts) == 3
    assert max(a.queue_seconds for a in result.execution_attempts) >= 0.01
    assert all(a.executor_seconds >= 0.01 for a in result.execution_attempts)


async def test_caller_cancellation_exposes_ledger_on_cancelled_error(host_session):
    service, run_id = host_session(profile="python_reviewer")
    entered = asyncio.Event()

    async def execute(job):
        job.observation.input_tokens = 9
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(HostReviewAdapter(service).run_primary_review(
        run_id, execute, lambda _: DeepReviewSignals()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert caught.value.execution_attempts[0].input_tokens == 9
    assert caught.value.execution_attempts[0].outcome == "cancelled"


async def test_successful_profile_is_checkpointed_before_its_wave_finishes(host_session, tmp_path):
    from qa_orchestrator.checkpoints import ProfileCheckpointStore

    service, run_id = host_session(bundle="python_backend")
    store = ProfileCheckpointStore(tmp_path / "checkpoints.sqlite")
    good_finished = asyncio.Event()
    release = asyncio.Event()

    async def execute(job):
        if job.profile == ReviewAgent.CODE_EXPLORER:
            good_finished.set()
            return report(job.profile)
        await release.wait()
        raise RuntimeError("Provider failed")

    adapter = HostReviewAdapter(
        service,
        HostExecutionPolicy(max_retries=0),
        checkpoint_store=store,
        executor_identity="fixture-v1",
    )
    task = asyncio.create_task(
        adapter.run_primary_review(run_id, execute, lambda _: DeepReviewSignals())
    )
    await asyncio.wait_for(good_finished.wait(), 1)
    await asyncio.sleep(0)
    assert len(store.list_keys()) == 1
    release.set()
    assert (await task).session.status == "partial"
    service, new_id = host_session(bundle="python_backend")
    called = []

    async def succeed(job):
        called.append(job.profile)
        return report(job.profile)

    recovered = await HostReviewAdapter(
        service, checkpoint_store=store, executor_identity="fixture-v1"
    ).run_primary_review(new_id, succeed, lambda _: DeepReviewSignals())
    assert not recovered.failures
    assert ReviewAgent.CODE_EXPLORER not in called
    assert recovered.reused_profiles == (ReviewAgent.CODE_EXPLORER,)
    assert len(recovered.session.profile_reports) == 3


async def test_checkpoint_rejects_different_executor_identity(host_session, tmp_path):
    from qa_orchestrator.checkpoints import ProfileCheckpointStore

    store = ProfileCheckpointStore(tmp_path / "checkpoints.sqlite")
    called = []

    async def execute(job):
        called.append(job.profile)
        return report(job.profile)

    for identity in ("v1", "v2"):
        service, run_id = host_session(profile="python_reviewer")
        await HostReviewAdapter(
            service, checkpoint_store=store, executor_identity=identity
        ).run_primary_review(run_id, execute, lambda _: DeepReviewSignals())
    assert len(called) == 2


async def test_retry_gets_a_fresh_checklist_after_executor_mutation(host_session):
    service, run_id = host_session(profile="python_reviewer")
    original_focus = service.prepare_review_route("python_reviewer").focus

    async def execute(job):
        assert job.route.focus == original_focus
        if job.attempt == 1:
            job.route.focus = "changed by executor"
            raise RetryableProfileError()
        return report(job.profile)

    result = await HostReviewAdapter(
        service, HostExecutionPolicy(retry_delay_seconds=0)
    ).run_primary_review(run_id, execute, lambda _: DeepReviewSignals())
    assert not result.failures


async def test_wave_barriers_and_fixed_order_are_enforced(host_session):
    service, run_id = host_session(bundle="ordinary_mr")
    explorer_started, release_explorer, release_second = (asyncio.Event() for _ in range(3))
    second_started = {
        p: asyncio.Event() for p in (ReviewAgent.CODE_REVIEWER, ReviewAgent.PR_TEST_ANALYZER)
    }

    async def execute(job):
        if job.profile == ReviewAgent.CODE_EXPLORER:
            explorer_started.set()
            await release_explorer.wait()
        else:
            assert job.previous_reports[0].profile == ReviewAgent.CODE_EXPLORER
            second_started[job.profile].set()
            await release_second.wait()
        return report(job.profile)

    task = asyncio.create_task(
        HostReviewAdapter(service).run_primary_review(
            run_id, execute, lambda reports: DeepReviewSignals()
        )
    )
    await asyncio.wait_for(explorer_started.wait(), 1)
    assert not any(event.is_set() for event in second_started.values())
    release_explorer.set()
    await asyncio.wait_for(asyncio.gather(*(e.wait() for e in second_started.values())), 1)
    assert service.get_qa_orchestration(run_id).completed_profiles == [ReviewAgent.CODE_EXPLORER]
    release_second.set()
    result = await task
    assert result.session.current_step == "synthesis"
    assert result.session.completed_profiles == [
        ReviewAgent.CODE_EXPLORER,
        ReviewAgent.CODE_REVIEWER,
        ReviewAgent.PR_TEST_ANALYZER,
    ]
    assert all(r.execution_metrics for r in result.session.profile_reports)


async def test_host_concurrency_limit_bounds_the_python_single_wave(host_session):
    service, run_id = host_session(bundle="python_backend")
    active = peak = 0

    async def execute(job):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return report(job.profile)

    result = await HostReviewAdapter(
        service, HostExecutionPolicy(max_concurrency=2)
    ).run_primary_review(run_id, execute, lambda reports: DeepReviewSignals())
    assert peak == 2
    assert len(result.session.completed_profiles) == 3


async def test_only_explicit_transient_failures_retry_and_retry_tokens_stay_unknown(host_session):
    service, run_id = host_session(profile="python_reviewer")
    attempts = []

    async def execute(job):
        attempts.append(job.attempt)
        if job.attempt == 1:
            raise RetryableProfileError()
        return ProfileReviewReport(
            profile=job.profile,
            scope="Reviewed source.",
            execution_metrics=ProfileExecutionMetrics(
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
                input_tokens=100,
                output_tokens=20,
            ),
        )

    adapter = HostReviewAdapter(service, HostExecutionPolicy(retry_delay_seconds=0))
    result = await adapter.run_primary_review(run_id, execute, lambda reports: DeepReviewSignals())
    assert attempts == [1, 2]
    metrics = result.session.profile_reports[0].execution_metrics
    assert metrics.attempt_count == 2
    assert metrics.input_tokens is None


async def test_permanent_failure_stops_partial_without_recording_a_false_group(host_session):
    service, run_id = host_session(bundle="python_backend")
    calls = []

    async def execute(job):
        calls.append(job.profile)
        if job.profile == ReviewAgent.PYTHON_REVIEWER:
            raise ValueError("private provider body")
        return report(job.profile)

    result = await HostReviewAdapter(service).run_primary_review(
        run_id, execute, lambda reports: DeepReviewSignals()
    )
    assert result.session.status == "partial"
    assert result.session.completed_profiles == []
    assert len(result.unsaved_reports) == 2
    assert result.failures[0].reason == "executor_error"
    assert len(calls) == 3


async def test_run_timeout_cancels_and_drains_workers(host_session):
    service, run_id = host_session(bundle="python_backend")
    stopped = []

    async def execute(job):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append(job.profile)

    adapter = HostReviewAdapter(service, HostExecutionPolicy(run_timeout_seconds=0.02))
    result = await adapter.run_primary_review(run_id, execute, lambda reports: DeepReviewSignals())
    assert result.session.status == "partial"
    assert result.failures[0].reason == "run_timeout"
    assert len(stopped) == 3


async def test_invalid_evidence_does_not_advance_the_group(host_session):
    service, run_id = host_session(profile="code_reviewer")

    async def execute(job):
        return ProfileReviewReport(
            profile=job.profile,
            scope="Reviewed source.",
            unverified=({"text": "Unknown source", "evidence_refs": ("E9",)},),
        )

    result = await HostReviewAdapter(service).run_primary_review(
        run_id, execute, lambda reports: DeepReviewSignals()
    )
    assert result.session.status == "partial"
    assert result.session.completed_profiles == []
    assert len(result.unsaved_reports) == 1


async def test_profile_timeout_is_bounded_and_does_not_complete_a_profile(host_session):
    service, run_id = host_session(profile="python_reviewer")
    calls = []

    async def execute(job):
        calls.append(job.attempt)
        await asyncio.Event().wait()

    policy = HostExecutionPolicy(profile_timeout_seconds=0.01, retry_delay_seconds=0)
    result = await HostReviewAdapter(service, policy).run_primary_review(
        run_id, execute, lambda reports: DeepReviewSignals()
    )
    assert calls == [1, 2]
    assert result.failures[0].reason == "profile_timeout"
    assert result.session.completed_profiles == []


async def test_cancelling_the_host_cancels_workers_and_records_partial(host_session):
    service, run_id = host_session(profile="python_reviewer")
    started, stopped = asyncio.Event(), asyncio.Event()

    async def execute(job):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    task = asyncio.create_task(
        HostReviewAdapter(service).run_primary_review(
            run_id, execute, lambda reports: DeepReviewSignals()
        )
    )
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()
    assert service.get_qa_orchestration(run_id).status == "partial"


async def test_lost_transition_response_is_retried_with_the_same_key(host_session, monkeypatch):
    service, run_id = host_session(profile="python_reviewer")
    advance = service.advance_qa_orchestration
    calls = []

    def lose_first_response(request):
        calls.append(request.request_id)
        result = advance(request)
        if len(calls) == 1:
            raise TimeoutError("response lost after commit")
        return result

    monkeypatch.setattr(service, "advance_qa_orchestration", lose_first_response)

    async def execute(job):
        return report(job.profile)

    result = await HostReviewAdapter(service).run_primary_review(
        run_id, execute, lambda reports: DeepReviewSignals()
    )
    assert len(calls) == 2 and calls[0] == calls[1]
    assert result.session.revision == 2
    assert result.session.completed_profiles == [ReviewAgent.PYTHON_REVIEWER]


async def test_a_stale_host_does_not_stop_a_session_advanced_by_another_host(host_session):
    service, run_id = host_session(profile="python_reviewer")
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(job):
        started.set()
        await release.wait()
        return report(job.profile)

    async def fast(job):
        return report(job.profile)

    older = asyncio.create_task(
        HostReviewAdapter(service).run_primary_review(
            run_id, slow, lambda reports: DeepReviewSignals()
        )
    )
    await asyncio.wait_for(started.wait(), 1)
    newer = await HostReviewAdapter(service).run_primary_review(
        run_id, fast, lambda reports: DeepReviewSignals()
    )
    release.set()
    with pytest.raises(OrchestrationError, match="stale session revision"):
        await older
    assert (
        service.get_qa_orchestration(run_id).current_step
        == newer.session.current_step
        == "synthesis"
    )


async def test_heartbeat_keeps_a_long_profile_alive(host_session):
    from qa_orchestrator.orchestration import QaOrchestrator

    service, _ = host_session(profile="python_reviewer")
    context = service.orchestrator.list_sessions().sessions[0]
    review_context = service.get_qa_orchestration(context.run_id).review_context
    service.orchestrator = QaOrchestrator(ttl_seconds=1, max_sessions=10)
    session = service.start_qa_orchestration("ordinary_review", review_context)
    service.advance_qa_orchestration(
        AdvanceQaOrchestrationRequest(
            run_id=session.run_id,
            completed_step="triage",
            status="completed",
            selected_profile="python_reviewer",
        )
    )

    async def execute(job):
        await asyncio.sleep(1.05)
        return report(job.profile)

    policy = HostExecutionPolicy(heartbeat_interval_seconds=0.1)
    result = await HostReviewAdapter(service, policy).run_primary_review(
        session.run_id, execute, lambda reports: DeepReviewSignals()
    )
    assert result.session.current_step == "synthesis"


async def test_local_evidence_verification_fails_before_starting_workers(host_session):
    service, run_id = host_session(profile="python_reviewer")

    def verify(context):
        raise ValueError("pinned content hash mismatch")

    async def execute(job):
        pytest.fail("worker must not start with invalid evidence")

    adapter = HostReviewAdapter(service, evidence_verifier=verify)
    with pytest.raises(ValueError, match="pinned content hash mismatch"):
        await adapter.run_primary_review(run_id, execute, lambda reports: DeepReviewSignals())
    assert service.get_qa_orchestration(run_id).completed_profiles == []
