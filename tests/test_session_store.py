import json
import os
import sqlite3
import stat
from datetime import UTC, datetime, timedelta

import pytest

from qa_orchestrator.contracts import (
    ProfileAttemptMetrics,
    ProfileContextSelection,
    ProfileExecutionMetrics,
    ReviewAgent,
    ReviewBundle,
)
from qa_orchestrator.model_policy import ModelProvider, ModelSelection
from qa_orchestrator.orchestration import (
    DeepReviewSignals,
    OrchestrationError,
    OrchestrationStep,
    QaOrchestrator,
)
from qa_orchestrator.session_store import SqliteSessionStore, inspect_session_store


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 28, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


def _orchestrator(store: SqliteSessionStore, clock: FakeClock) -> QaOrchestrator:
    return QaOrchestrator(
        ttl_seconds=60,
        max_sessions=10,
        session_store=store,
        clock=clock,
    )


def test_unfinished_session_survives_restart_and_transition_refreshes_ttl(tmp_path):
    store = SqliteSessionStore(tmp_path / "sessions.sqlite3")
    clock = FakeClock()
    orchestrator = _orchestrator(store, clock)
    started = orchestrator.start("ordinary_review")

    clock.advance(30)
    assert orchestrator.get(started.run_id).expires_at == started.expires_at
    clock.advance(15)
    advanced = orchestrator.advance(
        run_id=started.run_id,
        completed_step=OrchestrationStep.TRIAGE,
        status="completed",
        selected_profile=ReviewAgent.CODE_REVIEWER,
    )

    assert advanced.expires_at == clock() + timedelta(seconds=60)

    restored = _orchestrator(store, clock).get(started.run_id)

    assert restored.model_dump() == advanced.model_dump()


def test_completed_review_artifacts_survive_restart_until_normal_expiry(
    tmp_path,
    bound_review_context,
    review_finding_candidate,
    make_profile_review_report,
    make_final_review_report,
):
    store = SqliteSessionStore(tmp_path / "sessions.sqlite3")
    clock = FakeClock()
    first_process = _orchestrator(store, clock)
    started = first_process.start("ordinary_review", bound_review_context)
    selected = first_process.advance(
        run_id=started.run_id,
        completed_step=OrchestrationStep.TRIAGE,
        status="completed",
        selected_profile=ReviewAgent.CODE_REVIEWER,
    )
    attempt_started = clock()
    attempt_completed = clock() + timedelta(seconds=4)
    selection = ProfileContextSelection(
        source_packet_sha256=bound_review_context.evidence_packet_sha256,
        selected_packet_sha256="8" * 64,
        included_refs=bound_review_context.evidence_refs,
        changed_files_count=len(bound_review_context.changed_files),
        max_bytes=1024,
        selected_bytes=512,
    )
    execution_metrics = ProfileExecutionMetrics(
        started_at=attempt_started,
        completed_at=attempt_completed,
        input_tokens=900,
        output_tokens=160,
        attempts=(
            ProfileAttemptMetrics(
                profile=ReviewAgent.CODE_REVIEWER,
                attempt=1,
                outcome="completed",
                review_head_sha=bound_review_context.head_sha,
                review_diff_sha256=bound_review_context.diff_sha256,
                started_at=attempt_started,
                completed_at=attempt_completed,
                queue_seconds=0,
                executor_seconds=4,
                submitted_prompt_sha256="9" * 64,
                prompt_write_status="written",
                source_packet_sha256=bound_review_context.evidence_packet_sha256,
                selected_packet_sha256=selection.selected_packet_sha256,
                included_evidence_refs=selection.included_refs,
                omitted_evidence_refs=selection.omitted_refs,
                included_source_paths=(),
            ),
        ),
    )
    reviewed = first_process.advance(
        run_id=started.run_id,
        completed_step=selected.current_step,
        status="completed",
        completed_profile=ReviewAgent.CODE_REVIEWER,
        profile_reports=[
            make_profile_review_report(
                ReviewAgent.CODE_REVIEWER,
                context_selection=selection,
                findings=(review_finding_candidate,),
                execution_metrics=execution_metrics,
            )
        ],
        risk_signals=DeepReviewSignals(),
    )
    awaiting_outcome = first_process.advance(
        run_id=started.run_id,
        completed_step=reviewed.current_step,
        status="completed",
    )
    final_report = make_final_review_report(
        (ReviewAgent.CODE_REVIEWER,),
        findings=((review_finding_candidate, (ReviewAgent.CODE_REVIEWER,)),),
    )
    finished = first_process.finish(
        run_id=started.run_id,
        outcome="completed",
        final_review_report=final_report,
    )

    assert awaiting_outcome.current_step is OrchestrationStep.AWAITING_HOST_OUTCOME
    assert store.load_all() == [finished]

    restored = _orchestrator(store, clock)

    assert restored.get(started.run_id) == finished
    assert restored.get(started.run_id).profile_reports[0].execution_metrics == execution_metrics
    listed = restored.list_sessions().sessions[0]
    assert listed.review_repository == bound_review_context.repository
    assert listed.review_head_sha == bound_review_context.head_sha
    assert listed.review_diff_sha256 == bound_review_context.diff_sha256
    assert listed.has_review_artifacts is True

    legacy_payload = json.loads(finished.model_dump_json())
    for finding in legacy_payload["final_review_report"]["finding_candidates"]:
        finding.pop("source_candidates")
    legacy_payload["final_review_report"].pop("coverage_ledger")
    connection = sqlite3.connect(store.path)
    try:
        connection.execute(
            "UPDATE sessions SET payload = ? WHERE run_id = ?",
            (json.dumps(legacy_payload), started.run_id),
        )
        connection.commit()
    finally:
        connection.close()

    legacy_restored = _orchestrator(store, clock)
    legacy_report = legacy_restored.get(started.run_id).final_review_report
    assert legacy_report is not None
    assert legacy_report.finding_candidates[0].source_candidates == ()
    assert (
        legacy_restored.finish(
            run_id=started.run_id,
            outcome="completed",
            final_review_report=legacy_report,
        ).final_review_report
        == legacy_report
    )

    clock.advance(61)
    assert restored.list_sessions().sessions == []
    with pytest.raises(OrchestrationError, match="expired session"):
        restored.get(started.run_id)
    assert store.load_all() == []


def test_python_backend_session_resumes_remaining_group_after_schedule_change(tmp_path):
    store = SqliteSessionStore(tmp_path / "sessions.sqlite3")
    clock = FakeClock()
    first_process = _orchestrator(store, clock)
    started = first_process.start("ordinary_review")
    selected = first_process.advance(
        run_id=started.run_id,
        completed_step=OrchestrationStep.TRIAGE,
        status="completed",
        selected_bundle=ReviewBundle.PYTHON_BACKEND,
    )
    partially_completed = first_process.advance(
        run_id=selected.run_id,
        completed_step=OrchestrationStep.PRIMARY_REVIEW,
        status="completed",
        completed_profile=ReviewAgent.CODE_EXPLORER,
    )

    restored = _orchestrator(store, clock)
    assert restored.get(started.run_id) == partially_completed

    completed = restored.advance(
        run_id=started.run_id,
        completed_step=OrchestrationStep.PRIMARY_REVIEW,
        status="completed",
        completed_profiles=[
            ReviewAgent.PYTHON_REVIEWER,
            ReviewAgent.PR_TEST_ANALYZER,
        ],
    )

    audited = restored.advance(
        run_id=started.run_id,
        completed_step=OrchestrationStep.PRIMARY_REVIEW,
        status="completed",
        completed_profile=ReviewAgent.OMISSION_AUDITOR,
        risk_signals=DeepReviewSignals(),
    )

    assert completed.current_step is OrchestrationStep.PRIMARY_REVIEW
    assert completed.current_profile is ReviewAgent.OMISSION_AUDITOR
    assert audited.current_step is OrchestrationStep.SYNTHESIS
    assert audited.completed_profiles == [
        ReviewAgent.CODE_EXPLORER,
        ReviewAgent.PYTHON_REVIEWER,
        ReviewAgent.PR_TEST_ANALYZER,
        ReviewAgent.OMISSION_AUDITOR,
    ]


@pytest.mark.parametrize(
    "remaining_profiles",
    [
        pytest.param(
            [
                ReviewAgent.CODE_EXPLORER,
                ReviewAgent.PYTHON_REVIEWER,
                ReviewAgent.PR_TEST_ANALYZER,
            ],
            id="repeats-completed-prefix",
        ),
        pytest.param(
            [ReviewAgent.PR_TEST_ANALYZER, ReviewAgent.PYTHON_REVIEWER],
            id="wrong-order",
        ),
        pytest.param([ReviewAgent.PYTHON_REVIEWER], id="incomplete-suffix"),
    ],
)
def test_python_backend_session_rejects_invalid_remaining_group_after_restore(
    tmp_path,
    remaining_profiles,
):
    store = SqliteSessionStore(tmp_path / "sessions.sqlite3")
    clock = FakeClock()
    first_process = _orchestrator(store, clock)
    started = first_process.start("ordinary_review")
    selected = first_process.advance(
        run_id=started.run_id,
        completed_step=OrchestrationStep.TRIAGE,
        status="completed",
        selected_bundle=ReviewBundle.PYTHON_BACKEND,
    )
    partially_completed = first_process.advance(
        run_id=selected.run_id,
        completed_step=OrchestrationStep.PRIMARY_REVIEW,
        status="completed",
        completed_profile=ReviewAgent.CODE_EXPLORER,
    )
    restored = _orchestrator(store, clock)

    with pytest.raises(OrchestrationError, match="next execution group"):
        restored.advance(
            run_id=started.run_id,
            completed_step=OrchestrationStep.PRIMARY_REVIEW,
            status="completed",
            completed_profiles=remaining_profiles,
            risk_signals=DeepReviewSignals(),
        )

    assert restored.get(started.run_id) == partially_completed



def test_legacy_session_without_policy_mode_restores_as_configured(tmp_path):
    path = tmp_path / "sessions.sqlite3"
    store = SqliteSessionStore(path)
    selection = ModelSelection(
        provider=ModelProvider.OPENAI,
        triage_model="gpt-6-luna",
        primary_model="gpt-6-sol",
        deep_model="gpt-6-sol",
        synthesis_model="gpt-6-sol",
    )
    orchestrator = QaOrchestrator(
        ttl_seconds=60,
        max_sessions=10,
        model_selection=selection,
        session_store=store,
        clock=FakeClock(),
    )
    started = orchestrator.start("ordinary_review")
    payload = json.loads(started.model_dump_json())
    payload["model_policy"].pop("mode", None)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE sessions SET payload = ? WHERE run_id = ?",
            (json.dumps(payload), started.run_id),
        )

    restored = _orchestrator(store, FakeClock()).get(started.run_id)

    assert restored.model_policy.mode == "configured"
    assert restored.model_policy.provider is ModelProvider.OPENAI
    assert restored.model_policy.model == "gpt-6-luna"
    assert restored.model_policy.reasoning == "max"


def test_explicit_delete_removes_only_target_from_memory_and_recovery_storage(tmp_path):
    store = SqliteSessionStore(tmp_path / "sessions.sqlite3")
    clock = FakeClock()
    orchestrator = _orchestrator(store, clock)
    target = orchestrator.start("ordinary_review")
    retained = orchestrator.start("widget_review")
    clock.advance(30)

    first = orchestrator.delete(target.run_id)
    assert first.model_dump() == {"run_id": target.run_id, "deleted": True}
    assert orchestrator.delete(target.run_id) == first
    assert store.load_all() == [retained]
    assert orchestrator.get(retained.run_id) == retained
    restored = _orchestrator(store, clock)
    assert restored.get(retained.run_id) == retained
    assert [entry.run_id for entry in restored.list_sessions().sessions] == [retained.run_id]
    with pytest.raises(OrchestrationError, match="unknown run_id"):
        restored.get(target.run_id)


def test_delete_store_failure_preserves_memory_and_can_be_retried(tmp_path, monkeypatch):
    store = SqliteSessionStore(tmp_path / "sessions.sqlite3")
    clock = FakeClock()
    orchestrator = _orchestrator(store, clock)
    target = orchestrator.start("ordinary_review")
    retained = orchestrator.start("widget_review")

    def fail_delete(run_id):
        raise ValueError("unable to update session store")

    with monkeypatch.context() as patch:
        patch.setattr(store, "delete", fail_delete)
        with pytest.raises(ValueError, match="unable to update session store"):
            orchestrator.delete(target.run_id)
    assert orchestrator.get(target.run_id) == target
    assert orchestrator.get(retained.run_id) == retained
    assert {session.run_id for session in store.load_all()} == {target.run_id, retained.run_id}
    orchestrator.delete(target.run_id)
    assert store.load_all() == [retained]


def test_delete_expired_target_does_not_purge_other_expired_sessions(tmp_path):
    store = SqliteSessionStore(tmp_path / "sessions.sqlite3")
    clock = FakeClock()
    orchestrator = _orchestrator(store, clock)
    target = orchestrator.start("ordinary_review")
    retained = orchestrator.start("widget_review")
    clock.advance(60)

    orchestrator.delete(target.run_id)

    assert store.load_all() == [retained]


def test_list_recovers_unfinished_sessions_after_restart_without_store_writes(tmp_path):
    path = tmp_path / "sessions.sqlite3"
    store = SqliteSessionStore(path)
    clock = FakeClock()
    first_process = _orchestrator(store, clock)
    started = first_process.start("ordinary_review")
    first_process.advance(
        run_id=started.run_id,
        completed_step=OrchestrationStep.TRIAGE,
        status="completed",
        selected_profile=ReviewAgent.CODE_REVIEWER,
    )
    restored = _orchestrator(store, clock)
    before = path.read_bytes()

    entries = restored.list_sessions().sessions

    assert len(entries) == 1
    assert entries[0].run_id == started.run_id
    assert entries[0].current_step is OrchestrationStep.PRIMARY_REVIEW
    assert entries[0].selected_profile is ReviewAgent.CODE_REVIEWER
    assert restored.get(entries[0].run_id).current_step is OrchestrationStep.PRIMARY_REVIEW
    assert path.read_bytes() == before


def test_list_omits_expired_sessions_without_extending_ttl_or_deleting_store_rows(tmp_path):
    path = tmp_path / "sessions.sqlite3"
    store = SqliteSessionStore(path)
    clock = FakeClock()
    orchestrator = _orchestrator(store, clock)
    started = orchestrator.start("ordinary_review")
    before = path.read_bytes()

    clock.advance(59)
    assert orchestrator.list_sessions().sessions[0].expires_at == started.expires_at
    clock.advance(1)
    assert orchestrator.list_sessions().sessions == []
    assert store.load_all()[0].expires_at == started.expires_at
    assert path.read_bytes() == before


def test_final_outcome_is_not_written_to_the_session_store(tmp_path):
    store = SqliteSessionStore(tmp_path / "sessions.sqlite3")
    clock = FakeClock()
    orchestrator = _orchestrator(store, clock)
    started = orchestrator.start("ordinary_review")
    stopped = orchestrator.advance(
        run_id=started.run_id,
        completed_step=OrchestrationStep.TRIAGE,
        status="partial",
    )

    assert store.load_all() == [stopped]
    finalized = orchestrator.finish(run_id=started.run_id, outcome="partial")

    assert store.load_all() == []
    assert orchestrator.finish(run_id=started.run_id, outcome="partial") == finalized
    with pytest.raises(OrchestrationError, match="unknown run_id"):
        _orchestrator(store, clock).get(started.run_id)


def test_expired_sessions_are_removed_during_restart(tmp_path):
    store = SqliteSessionStore(tmp_path / "sessions.sqlite3")
    clock = FakeClock()
    orchestrator = _orchestrator(store, clock)
    started = orchestrator.start("ordinary_review")
    another = orchestrator.start("ordinary_review")

    clock.advance(60)
    _orchestrator(store, clock)

    assert store.load_all() == []
    for session in (started, another):
        with pytest.raises(OrchestrationError, match="unknown run_id"):
            _orchestrator(store, clock).get(session.run_id)


def test_store_can_restore_exactly_maximum_active_sessions(tmp_path):
    store = SqliteSessionStore(tmp_path / "sessions.sqlite3")
    clock = FakeClock()
    orchestrator = QaOrchestrator(
        ttl_seconds=60,
        max_sessions=2,
        session_store=store,
        clock=clock,
    )
    first = orchestrator.start("ordinary_review")
    second = orchestrator.start("ordinary_review")

    restored = QaOrchestrator(
        ttl_seconds=60,
        max_sessions=2,
        session_store=store,
        clock=clock,
    )

    assert restored.get(first.run_id).run_id == first.run_id
    assert restored.get(second.run_id).run_id == second.run_id


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission modes are not available")
def test_session_store_database_has_private_permissions(tmp_path):
    path = tmp_path / "sessions.sqlite3"
    store = SqliteSessionStore(path)
    _orchestrator(store, FakeClock()).start("ordinary_review")

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_session_store_rejects_symlink_path(tmp_path):
    target = tmp_path / "target.sqlite3"
    target.touch()
    link = tmp_path / "sessions.sqlite3"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="regular file"):
        SqliteSessionStore(link)


def test_store_rejects_naive_expiry_before_recovery(tmp_path):
    path = tmp_path / "sessions.sqlite3"
    store = SqliteSessionStore(path)
    session = _orchestrator(store, FakeClock()).start("ordinary_review")
    payload = session.model_copy(
        update={"expires_at": session.expires_at.replace(tzinfo=None)}
    ).model_dump_json()
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE sessions SET payload = ?", (payload,))

    with pytest.raises(ValueError, match="invalid session data"):
        inspect_session_store(path)
    with pytest.raises(ValueError, match="invalid session data"):
        _orchestrator(store, FakeClock())


def test_store_rejects_duplicate_schema_metadata(tmp_path):
    path = tmp_path / "sessions.sqlite3"
    SqliteSessionStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO session_store_metadata VALUES (2)")
    before = path.read_bytes()

    with pytest.raises(ValueError, match="unsupported session store schema"):
        SqliteSessionStore(path)

    assert path.read_bytes() == before
