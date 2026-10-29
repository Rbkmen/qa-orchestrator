import asyncio
from datetime import UTC, datetime

import pytest

from qa_orchestrator.contracts import (
    FinalReviewReport,
    ProfileContextSelection,
    ProfileExecutionMetrics,
    ProfileReviewReport,
    ReviewAgent,
    ReviewContext,
    ReviewDiffHunk,
    ReviewScenarioDisposition,
    ReviewVerificationRecord,
    review_diff_hunk_inventory_sha256,
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

    def start(
        *, bundle=None, profile=None, implementation_review=None,
        acknowledge_limited_profile=False,
    ):
        session = service.start_qa_orchestration("ordinary_review", bound_review_context)
        service.advance_qa_orchestration(
            AdvanceQaOrchestrationRequest(
                run_id=session.run_id,
                completed_step="triage",
                status="completed",
                selected_bundle=bundle,
                selected_profile=profile,
                implementation_review=implementation_review,
                acknowledge_limited_profile=acknowledge_limited_profile,
            )
        )
        return service, session.run_id

    return start


def report(profile, context):
    assert context is not None
    available_source_paths = {
        entry.path
        for entry in context.evidence_registry
        if entry.kind == "source"
        and entry.revision == context.head_sha
        and entry.source_text_available is True
    }
    return ProfileReviewReport(
        profile=profile,
        context_selection=ProfileContextSelection(
            source_packet_sha256=context.evidence_packet_sha256,
            selected_packet_sha256=context.evidence_packet_sha256,
            included_refs=context.evidence_refs,
            changed_files_count=len(context.changed_files),
            source_excerpt_files_available=len(available_source_paths),
            source_excerpt_files_included=len(available_source_paths),
            max_bytes=1024,
            selected_bytes=512,
        ),
        scope="Pinned source inspected.",
        reviewed_changed_paths=context.changed_files,
    )


async def test_synthesis_replay_holds_upstream_reports_fixed_and_does_not_persist(
    host_session, make_final_review_report,
):
    service, run_id = host_session(profile=ReviewAgent.PYTHON_REVIEWER)
    adapter = HostReviewAdapter(service)

    async def execute_profile(job):
        return report(job.profile, job.session.review_context)

    await adapter.run_primary_review(run_id, execute_profile, lambda _: DeepReviewSignals())
    before = service.get_qa_orchestration(run_id)
    replay_jobs = []

    async def execute_synthesis(job):
        replay_jobs.append(job)
        return make_final_review_report(
            job.inputs.completed_profiles, context=job.inputs.review_context
        )

    first = await adapter.replay_synthesis(
        run_id,
        prompt_id="synthesis-v1",
        prompt="Original synthesis prompt",
        executor=execute_synthesis,
    )
    second = await adapter.replay_synthesis(
        run_id,
        prompt_id="synthesis-v2",
        prompt="Revised synthesis prompt",
        executor=execute_synthesis,
    )
    after = service.get_qa_orchestration(run_id)

    assert first.inputs_sha256 == second.inputs_sha256
    assert first.prompt_sha256 != second.prompt_sha256
    assert replay_jobs[0].inputs.profile_reports == tuple(before.profile_reports)
    assert replay_jobs[0].inputs.review_context == before.review_context
    assert not hasattr(replay_jobs[0].inputs, "final_review_report")
    assert after == before


async def test_synthesis_replay_rejects_a_report_for_another_profile(
    host_session, make_final_review_report,
):
    service, run_id = host_session(profile=ReviewAgent.PYTHON_REVIEWER)
    adapter = HostReviewAdapter(service)

    async def execute_profile(job):
        return report(job.profile, job.session.review_context)

    await adapter.run_primary_review(run_id, execute_profile, lambda _: DeepReviewSignals())
    before = service.get_qa_orchestration(run_id)

    async def execute_synthesis(job):
        return make_final_review_report(
            (ReviewAgent.CODE_REVIEWER,), context=job.inputs.review_context
        )

    with pytest.raises(ValueError, match="fixed profile reports"):
        await adapter.replay_synthesis(
            run_id,
            prompt_id="wrong-profile",
            prompt="Test prompt",
            executor=execute_synthesis,
        )

    assert service.get_qa_orchestration(run_id) == before


async def test_synthesis_replay_rejects_a_report_with_wrong_rules_digest(
    host_session, make_final_review_report,
):
    service, run_id = host_session(profile=ReviewAgent.PYTHON_REVIEWER)
    adapter = HostReviewAdapter(service)

    async def execute_profile(job):
        return report(job.profile, job.session.review_context)

    await adapter.run_primary_review(run_id, execute_profile, lambda _: DeepReviewSignals())

    async def execute_synthesis(job):
        valid = make_final_review_report(
            job.inputs.completed_profiles, context=job.inputs.review_context
        )
        return FinalReviewReport.model_validate({
            **valid.model_dump(mode="python"),
            "review_rules_sha256": "a" * 64,
        })

    with pytest.raises(ValueError, match="rules digest mismatch"):
        await adapter.replay_synthesis(
            run_id,
            prompt_id="wrong-rules-digest",
            prompt="Test prompt",
            executor=execute_synthesis,
        )


async def test_synthesis_replay_rejects_findings_not_linked_to_profile_reports(
    host_session, make_final_review_report, review_finding_candidate,
):
    service, run_id = host_session(profile=ReviewAgent.PYTHON_REVIEWER)
    adapter = HostReviewAdapter(service)

    async def execute_profile(job):
        return report(job.profile, job.session.review_context)

    await adapter.run_primary_review(run_id, execute_profile, lambda _: DeepReviewSignals())

    async def execute_synthesis(job):
        return make_final_review_report(
            job.inputs.completed_profiles,
            findings=((review_finding_candidate, (ReviewAgent.CODE_REVIEWER,)),),
            context=job.inputs.review_context,
        )

    with pytest.raises(ValueError, match="source candidates do not match profile reports"):
        await adapter.replay_synthesis(
            run_id,
            prompt_id="unlinked-finding",
            prompt="Test prompt",
            executor=execute_synthesis,
        )


async def test_synthesis_replay_rejects_verification_for_another_head(
    host_session, make_final_review_report,
):
    service, run_id = host_session(profile=ReviewAgent.PYTHON_REVIEWER)
    adapter = HostReviewAdapter(service)

    async def execute_profile(job):
        return report(job.profile, job.session.review_context)

    await adapter.run_primary_review(run_id, execute_profile, lambda _: DeepReviewSignals())

    async def execute_synthesis(job):
        valid = make_final_review_report(
            job.inputs.completed_profiles, context=job.inputs.review_context
        )
        return FinalReviewReport.model_validate({
            **valid.model_dump(mode="python"),
            "verification_records": ({
                "verification_id": "V-01",
                "check_type": "runtime",
                "scenario": "Service startup",
                "status": "passed",
                "head_sha": "9" * 40,
                "environment": "local",
                "source": "runtime_system",
                "source_ref": "local-startup",
                "evidence_refs": (),
            },),
        })

    with pytest.raises(ValueError, match="verification record does not match the bound head"):
        await adapter.replay_synthesis(
            run_id,
            prompt_id="wrong-verification-head",
            prompt="Test prompt",
            executor=execute_synthesis,
        )


async def test_unresolved_single_profile_finishes_without_scheduling_auditor(host_session):
    service, run_id = host_session(profile=ReviewAgent.PYTHON_REVIEWER, implementation_review=True)
    executed = []

    async def execute(job):
        executed.append(job.profile)
        return ProfileReviewReport(
            profile=job.profile,
            scope="Reviewed retry behavior.",
            reviewed_changed_paths=job.session.review_context.changed_files,
            scenario_coverage=(ReviewScenarioDisposition(
                scenario="A retry executes the write twice.", disposition="unverified",
                evidence_refs=("E1",), rationale="Idempotency is not established.",
            ),),
        )

    result = await HostReviewAdapter(service).run_primary_review(
        run_id, execute, lambda _: DeepReviewSignals()
    )

    assert executed == [ReviewAgent.PYTHON_REVIEWER]
    assert not result.failures
    assert result.session.current_step == "synthesis"
    assert result.session.profile_reports[0].scenario_coverage[0].disposition == "unverified"


async def test_host_reports_changed_paths_missing_from_profile_attestation(host_session):
    service, run_id = host_session(profile="python_reviewer")

    async def execute(job):
        return ProfileReviewReport(profile=job.profile, scope="Reviewed partial context.")

    result = await HostReviewAdapter(service).run_primary_review(
        run_id, execute, lambda _: DeepReviewSignals()
    )
    gaps = result.session.profile_reports[0].coverage_gaps
    assert any(
        "explicitly attest inspection" in gap.text and "src/service.py" in gap.text
        for gap in gaps
    )


async def test_host_assesses_risk_after_single_implementation_profile(host_session):
    service, run_id = host_session(profile=ReviewAgent.PYTHON_REVIEWER, implementation_review=True)
    seen = []

    async def execute(job):
        return report(job.profile, job.session.review_context)

    def assess(reports):
        seen.extend(item.profile for item in reports)
        return DeepReviewSignals()

    result = await HostReviewAdapter(service).run_primary_review(run_id, execute, assess)
    assert seen == [ReviewAgent.PYTHON_REVIEWER]
    assert result.session.current_step == "synthesis"




def test_host_rejects_attestation_for_path_outside_bound_change(bound_review_context):
    invalid = ProfileReviewReport(
        profile="python_reviewer",
        scope="Invalid path attestation.",
        reviewed_changed_paths=("src/other.py",),
    )

    with pytest.raises(ValueError, match="outside the bound change"):
        HostReviewAdapter._with_changed_path_coverage_gaps(bound_review_context, invalid)


def test_host_rejects_hunk_attestation_outside_bound_diff(bound_review_context):
    invalid = report(ReviewAgent.PYTHON_REVIEWER, bound_review_context).model_copy(
        update={"reviewed_diff_hunk_indexes": (0,)}
    )

    with pytest.raises(ValueError, match="hunk outside the bound diff"):
        HostReviewAdapter._validate_report(
            bound_review_context, ReviewAgent.PYTHON_REVIEWER, invalid
        )


def test_host_reports_unattested_hunks_when_inventory_is_complete(bound_review_context):
    hunks = (
        ReviewDiffHunk(
            index=0,
            changed_path_index=0,
            old_start=1,
            old_count=1,
            new_start=1,
            new_count=1,
            header="@@ -1,1 +1,1 @@",
            sha256="a" * 64,
        ),
        ReviewDiffHunk(
            index=1,
            changed_path_index=0,
            old_start=3,
            old_count=1,
            new_start=3,
            new_count=1,
            header="@@ -3,1 +3,1 @@",
            sha256="b" * 64,
        ),
    )
    context = ReviewContext.model_validate(
        {
            **bound_review_context.model_dump(mode="python"),
            "diff_hunks": hunks,
            "diff_hunk_inventory_sha256": review_diff_hunk_inventory_sha256(
                bound_review_context.diff_sha256, hunks
            ),
            "diff_hunk_inventory_status": "complete",
            "diff_hunk_inventory_count_at_least": len(hunks),
        }
    )
    incomplete = ProfileReviewReport(
        profile=ReviewAgent.PYTHON_REVIEWER,
        scope="Reviewed changed paths but omitted hunk attestations.",
        reviewed_changed_paths=context.changed_files,
    )

    augmented = HostReviewAdapter._with_changed_path_coverage_gaps(context, incomplete)

    assert any(
        "2 diff hunk(s)" in gap.text and "0, 1" in gap.text
        for gap in augmented.coverage_gaps
    )
    complete = incomplete.model_copy(update={"reviewed_diff_hunk_indexes": (0, 1)})
    fully_attested = HostReviewAdapter._with_changed_path_coverage_gaps(context, complete)
    assert not any("diff hunk(s)" in gap.text for gap in fully_attested.coverage_gaps)
    missing_both = incomplete.model_copy(update={"reviewed_changed_paths": ()})
    combined = HostReviewAdapter._with_changed_path_coverage_gaps(context, missing_both)
    host_gaps = [
        gap
        for gap in combined.coverage_gaps
        if "changed path(s)" in gap.text and "diff hunk(s)" in gap.text
    ]
    assert len(host_gaps) == 1


def test_host_does_not_add_hunk_gap_for_partial_inventory(bound_review_context):
    hunk = ReviewDiffHunk(
        index=0,
        changed_path_index=0,
        old_start=1,
        old_count=1,
        new_start=1,
        new_count=1,
        header="@@ -1,1 +1,1 @@",
        sha256="a" * 64,
    )
    context = ReviewContext.model_validate(
        {
            **bound_review_context.model_dump(mode="python"),
            "diff_hunks": (hunk,),
            "diff_hunk_inventory_sha256": review_diff_hunk_inventory_sha256(
                bound_review_context.diff_sha256, (hunk,)
            ),
            "diff_hunk_inventory_status": "partial",
            "diff_hunk_inventory_count_at_least": 2,
        }
    )
    reviewed = ProfileReviewReport(
        profile=ReviewAgent.PYTHON_REVIEWER,
        scope="Reviewed the indexed hunk.",
        reviewed_changed_paths=context.changed_files,
        reviewed_diff_hunk_indexes=(0,),
    )

    augmented = HostReviewAdapter._with_changed_path_coverage_gaps(context, reviewed)

    assert not any("diff hunk(s)" in gap.text for gap in augmented.coverage_gaps)


async def test_finish_review_exports_markdown_with_the_accepted_outcome(
    host_session,
    make_impact_coverage,
    tmp_path,
):
    service, run_id = host_session(profile="python_reviewer")
    adapter = HostReviewAdapter(service)

    async def execute(job):
        result = report(job.profile, job.session.review_context)
        selection = result.context_selection
        observation = job.observation
        observation.submitted_prompt_sha256 = "8" * 64
        observation.prompt_write_status = "written"
        observation.source_packet_sha256 = selection.source_packet_sha256
        observation.selected_packet_sha256 = selection.selected_packet_sha256
        observation.included_evidence_refs = selection.included_refs
        observation.omitted_evidence_refs = selection.omitted_refs
        observation.included_source_paths = tuple(
            sorted(
                {
                    entry.path
                    for entry in job.session.review_context.evidence_registry
                    if entry.kind == "source"
                    and entry.revision == job.session.review_context.head_sha
                    and entry.source_text_available is True
                    and entry.path in job.session.review_context.changed_files
                    and entry.evidence_ref in selection.included_refs
                }
            )
        )
        return result

    await adapter.run_primary_review(run_id, execute, lambda _: DeepReviewSignals())
    current = service.get_qa_orchestration(run_id)
    output = tmp_path / "review.md"
    output.write_text("previous artifact", encoding="utf-8")
    final_report = FinalReviewReport(
        summary="Review completed against the pinned context.",
        profiles_reviewed=(ReviewAgent.PYTHON_REVIEWER,),
        impact_coverage=make_impact_coverage(current.review_context),
        verification_records=(
            ReviewVerificationRecord(
                verification_id="V-01",
                check_type="ci",
                scenario="focused test suite",
                status="passed",
                head_sha=current.review_context.head_sha,
                environment="CI",
                source="ci_provider",
                source_ref="workflow 123 / job 456",
            ),
        ),
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
    assert finished.final_review_report is not None
    assert finished.final_review_report.model_copy(update={"coverage_ledger": None}) == final_report
    assert output.read_text(encoding="utf-8").startswith("# QA review\n")
    assert "**Outcome:** completed" in output.read_text(encoding="utf-8")
    assert "Review completed against the pinned context" in output.read_text(encoding="utf-8")
    assert "provenance: host reported, unverified" in output.read_text(encoding="utf-8")


async def test_attempt_ledger_preserves_failed_usage_and_sums_known_retries(host_session):
    from qa_orchestrator.codex_executor import RetryableProfileOutputError

    service, run_id = host_session(profile="python_reviewer")

    async def execute(job):
        job.observation.input_tokens = 10 * job.attempt
        job.observation.output_tokens = 2 * job.attempt
        if job.attempt == 1:
            raise RetryableProfileOutputError("secret")
        return report(job.profile, job.session.review_context)

    result = await HostReviewAdapter(
        service, HostExecutionPolicy(retry_delay_seconds=0)
    ).run_primary_review(run_id, execute, lambda _: DeepReviewSignals())
    assert [a.outcome for a in result.execution_attempts] == ["retryable_error", "completed"]
    metrics = result.session.profile_reports[0].execution_metrics
    assert metrics.input_tokens == 30
    assert metrics.output_tokens == 6
    assert metrics.attempts == result.execution_attempts
    context = service.get_qa_orchestration(run_id).review_context
    assert result.execution_attempts[0].review_head_sha == context.head_sha
    assert result.execution_attempts[0].review_diff_sha256 == context.diff_sha256
    assert result.execution_attempts[1].included_evidence_refs == ()
    assert result.execution_attempts[1].selected_packet_sha256 is None
    assert result.execution_attempts[1].prompt_write_status is None
    assert "secret" not in str(result.execution_attempts)


async def test_attempt_ledger_keeps_sanitized_cli_failure_metadata(host_session):
    service, run_id = host_session(profile="python_reviewer")

    async def execute(job):
        job.observation.cli_failure_detail = (
            "Error: token=sk-1234567890abcdef access_token=private-value "
            "at /Users/private/.codex/log"
        )
        job.observation.cli_exit_code = 2
        job.observation.cli_event_type = "turn.failed"
        raise ValueError("private provider response")

    result = await HostReviewAdapter(
        service, HostExecutionPolicy(retry_delay_seconds=0)
    ).run_primary_review(run_id, execute, lambda _: DeepReviewSignals())

    attempt = result.execution_attempts[0]
    assert attempt.outcome == "failed"
    assert attempt.cli_failure_detail == (
        "Error: token=[REDACTED] access_token=[REDACTED] at [path]"
    )
    assert attempt.cli_exit_code == 2
    assert attempt.cli_event_type == "turn.failed"
    assert "private provider response" not in str(result.execution_attempts)


async def test_cancelled_attempt_keeps_observed_usage_in_partial_result(host_session):
    service, run_id = host_session(profile="python_reviewer")

    async def execute(job):
        job.observation.input_tokens = 17
        await asyncio.Event().wait()

    result = await HostReviewAdapter(
        service, HostExecutionPolicy(run_timeout_seconds=0.02)
    ).run_primary_review(run_id, execute, lambda _: DeepReviewSignals())
    assert result.session.status == "partial"
    assert result.session.stop_reason == "host: run_timeout"
    assert result.execution_attempts[0].outcome == "cancelled"
    assert result.execution_attempts[0].failure_kind == "cancelled"
    assert result.execution_attempts[0].input_tokens == 17
    assert result.execution_attempts[0].output_tokens is None


async def test_attempt_ledger_reports_semaphore_wait(host_session):
    service, run_id = host_session(bundle="python_backend")

    async def execute(job):
        # Leave timing headroom for Windows timer granularity and runner load.
        await asyncio.sleep(0.05)
        return report(job.profile, job.session.review_context)

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
            return report(job.profile, job.session.review_context)
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
        return report(job.profile, job.session.review_context)

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
        return report(job.profile, job.session.review_context)

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
        return report(job.profile, job.session.review_context)

    result = await HostReviewAdapter(
        service, HostExecutionPolicy(retry_delay_seconds=0)
    ).run_primary_review(run_id, execute, lambda _: DeepReviewSignals())
    assert not result.failures


async def test_wave_barriers_and_fixed_order_are_enforced(host_session):
    service, run_id = host_session(bundle="ordinary_mr")
    explorer_started, release_explorer, release_second = (
        asyncio.Event() for _ in range(3)
    )
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
        return report(job.profile, job.session.review_context)

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
        return report(job.profile, job.session.review_context)

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
            reviewed_changed_paths=job.session.review_context.changed_files,
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
        return report(job.profile, job.session.review_context)

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
        return report(job.profile, job.session.review_context)

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
        return report(job.profile, job.session.review_context)

    async def fast(job):
        return report(job.profile, job.session.review_context)

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
    result = await older
    assert [failure.reason for failure in result.failures] == [
        "report_or_assessment_error", "session_revision_conflict"
    ]
    assert len(result.unsaved_reports) == 1
    assert result.unsaved_reports[0].profile == ReviewAgent.PYTHON_REVIEWER
    assert result.session_snapshot_fresh is True
    assert len(result.execution_attempts) == 1
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
        return report(job.profile, job.session.review_context)

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


async def test_invalid_output_is_retained_privately_across_a_retry(host_session):
    from qa_orchestrator.host import ProfileOutputError, RetryableProfileError

    class InvalidOutput(RetryableProfileError, ProfileOutputError):
        pass

    service, run_id = host_session(profile="python_reviewer")
    secret = b"private diagnostic output"
    async def execute(job):
        if job.attempt == 1:
            raise InvalidOutput("invalid", retained_output=secret)
        return report(job.profile, job.session.review_context)

    result = await HostReviewAdapter(
        service, HostExecutionPolicy(retry_delay_seconds=0)
    ).run_primary_review(run_id, execute, lambda reports: DeepReviewSignals())
    assert not result.failures
    assert len(result.retained_outputs) == 1
    assert result.retained_outputs[0].attempt == 1
    assert result.retained_outputs[0].raw_output == secret
    assert secret.decode() not in repr(result)
    assert secret.decode() not in result.session.model_dump_json()


async def test_stop_failure_returns_unsaved_reports_and_marks_snapshot_unavailable(host_session, monkeypatch):
    service, run_id = host_session(profile="python_reviewer")
    original_get = service.get_qa_orchestration
    calls = 0

    def unavailable_after_start(requested_run_id):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise ConnectionError("private details")
        return original_get(requested_run_id)

    monkeypatch.setattr(service, "get_qa_orchestration", unavailable_after_start)

    async def execute(job):
        return report(job.profile, job.session.review_context)

    def fail_assessment(reports):
        raise ValueError("private details")

    result = await HostReviewAdapter(service).run_primary_review(run_id, execute, fail_assessment)
    assert [failure.reason for failure in result.failures] == [
        "report_or_assessment_error", "session_stop_failed", "session_unavailable"
    ]
    assert result.session_snapshot_fresh is False
    assert len(result.unsaved_reports) == 1
    assert len(result.execution_attempts) == 1
    assert original_get(run_id).current_step == "primary_review"


async def test_exhausted_lost_transition_response_returns_reports_without_stopping_new_state(host_session, monkeypatch):
    service, run_id = host_session(profile="python_reviewer")
    advance = service.advance_qa_orchestration

    def lose_responses(request):
        advance(request)
        raise TimeoutError("private provider details")

    monkeypatch.setattr(service, "advance_qa_orchestration", lose_responses)

    async def execute(job):
        return report(job.profile, job.session.review_context)

    result = await HostReviewAdapter(service).run_primary_review(
        run_id, execute, lambda _: DeepReviewSignals()
    )
    assert result.failures[0].reason == "transition_response_unavailable"
    assert result.session_snapshot_fresh is True
    assert result.session.current_step == "synthesis"
    assert len(result.unsaved_reports) == 1
    assert len(result.execution_attempts) == 1


async def test_cancellation_retains_completed_profiles_even_if_stop_fails(host_session, monkeypatch):
    service, run_id = host_session(bundle="python_backend")
    ready = asyncio.Event()
    completed = 0
    adapter = HostReviewAdapter(service)

    async def execute(job):
        nonlocal completed
        if job.profile == ReviewAgent.PYTHON_REVIEWER:
            await asyncio.Event().wait()
        completed += 1
        if completed == 2:
            ready.set()
        return report(job.profile, job.session.review_context)

    def stop_fails(*args):
        raise ConnectionError("private details")

    monkeypatch.setattr(adapter, "_stop_partial", stop_fails)
    task = asyncio.create_task(adapter.run_primary_review(run_id, execute, lambda _: DeepReviewSignals()))
    await asyncio.wait_for(ready.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert len(caught.value.unsaved_reports) == 2
    assert len(caught.value.execution_attempts) == 3


async def test_expired_session_returns_reports_and_marks_snapshot_unavailable(host_session):
    from datetime import timedelta

    service, run_id = host_session(profile="python_reviewer")
    current = service.get_qa_orchestration(run_id)

    async def execute(job):
        service.orchestrator._clock = lambda: current.expires_at + timedelta(seconds=1)
        return report(job.profile, job.session.review_context)

    result = await HostReviewAdapter(service).run_primary_review(
        run_id, execute, lambda _: DeepReviewSignals()
    )
    assert result.session_snapshot_fresh is False
    assert result.failures[-1].reason == "session_unavailable"
    assert len(result.unsaved_reports) == 1
    assert len(result.execution_attempts) == 1


async def test_storage_write_failure_returns_reports_and_fresh_session(host_session, monkeypatch):
    service, run_id = host_session(profile="python_reviewer")

    def fail_write(session, **kwargs):
        raise ValueError("private storage details")

    monkeypatch.setattr(service.orchestrator, "_save_session", fail_write)

    async def execute(job):
        return report(job.profile, job.session.review_context)

    result = await HostReviewAdapter(service).run_primary_review(
        run_id, execute, lambda _: DeepReviewSignals()
    )
    assert [failure.reason for failure in result.failures] == [
        "report_or_assessment_error", "session_stop_failed"
    ]
    assert result.session_snapshot_fresh is True
    assert result.session.current_step == "primary_review"
    assert len(result.unsaved_reports) == 1
    assert len(result.execution_attempts) == 1


async def test_concurrent_terminal_transition_is_reported_as_conflict(host_session):
    service, run_id = host_session(profile="python_reviewer")
    ready, release = asyncio.Event(), asyncio.Event()

    async def execute(job):
        ready.set()
        await release.wait()
        return report(job.profile, job.session.review_context)

    task = asyncio.create_task(HostReviewAdapter(service).run_primary_review(
        run_id, execute, lambda _: DeepReviewSignals()
    ))
    await asyncio.wait_for(ready.wait(), 1)
    current = service.get_qa_orchestration(run_id)
    terminal = service.advance_qa_orchestration(AdvanceQaOrchestrationRequest(
        run_id=run_id, completed_step="primary_review", status="partial",
        expected_revision=current.revision,
    ))
    release.set()
    result = await task
    assert result.session.status == "partial"
    assert result.session.revision == terminal.revision
    assert any(failure.reason == "session_revision_conflict" for failure in result.failures)
    assert len(result.unsaved_reports) == 1
