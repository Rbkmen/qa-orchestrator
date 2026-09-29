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
