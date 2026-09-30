import os
import sqlite3
import stat
from datetime import UTC, datetime, timedelta

import pytest

from qa_orchestrator.contracts import ReviewAgent
from qa_orchestrator.orchestration import (
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
