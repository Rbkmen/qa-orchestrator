from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from qa_orchestrator.orchestration import OrchestrationError, QaOrchestrator
from qa_orchestrator.session_store import SqliteSessionStore


class Clock:
    def __init__(self):
        self.now = datetime.now(UTC)

    def __call__(self):
        return self.now


def request(run_id):
    return {
        "run_id": run_id,
        "completed_step": "triage",
        "status": "completed",
        "selected_profile": "code_reviewer",
        "request_id": "triage-1",
        "expected_revision": 0,
    }


def test_exact_retry_returns_retained_state_without_extending_the_lease():
    clock = Clock()
    orchestrator = QaOrchestrator(ttl_seconds=60, max_sessions=10, clock=clock)
    started = orchestrator.start("ordinary_review")
    advanced = orchestrator.advance(**request(started.run_id))
    clock.now += timedelta(seconds=20)
    replay = orchestrator.advance(**request(started.run_id))
    assert replay == advanced
    assert replay.revision == 1
    with pytest.raises(OrchestrationError, match="different transition"):
        orchestrator.advance(**{**request(started.run_id), "selected_profile": "python_reviewer"})
    with pytest.raises(OrchestrationError, match="stale session revision"):
        orchestrator.advance(
            run_id=started.run_id,
            completed_step="primary_review",
            status="partial",
            request_id="stop-1",
            expected_revision=0,
        )
    assert orchestrator.get(started.run_id) == advanced


def test_keyed_replay_after_later_progress_returns_current_state():
    orchestrator = QaOrchestrator(ttl_seconds=60, max_sessions=10)
    started = orchestrator.start("ordinary_review")
    orchestrator.advance(**request(started.run_id))
    stopped = orchestrator.advance(
        run_id=started.run_id,
        completed_step="primary_review",
        status="partial",
        request_id="stop-1",
        expected_revision=1,
    )
    assert orchestrator.advance(**request(started.run_id)) == stopped
    assert stopped.revision == 2


def test_concurrent_exact_retries_apply_once():
    orchestrator = QaOrchestrator(ttl_seconds=60, max_sessions=10)
    started = orchestrator.start("ordinary_review")
    with ThreadPoolExecutor(max_workers=4) as workers:
        outputs = list(
            workers.map(lambda _: orchestrator.advance(**request(started.run_id)), range(8))
        )
    assert all(output == outputs[0] for output in outputs)
    assert outputs[0].revision == 1
    assert len(outputs[0].transition_receipts) == 1


def test_receipts_survive_restart_and_storage_failure_does_not_consume_a_key(tmp_path, monkeypatch):
    clock = Clock()
    store = SqliteSessionStore(tmp_path / "sessions.sqlite3")
    first = QaOrchestrator(ttl_seconds=60, max_sessions=10, clock=clock, session_store=store)
    started = first.start("ordinary_review")
    save = store.save

    def fail(session):
        raise ValueError("storage unavailable")

    monkeypatch.setattr(store, "save", fail)
    with pytest.raises(ValueError, match="storage unavailable"):
        first.advance(**request(started.run_id))
    assert first.get(started.run_id).revision == 0
    monkeypatch.setattr(store, "save", save)
    advanced = first.advance(**request(started.run_id))
    restored = QaOrchestrator(ttl_seconds=60, max_sessions=10, clock=clock, session_store=store)
    assert restored.advance(**request(started.run_id)) == advanced


def test_renewal_changes_only_the_lease_and_cannot_revive_terminal_or_expired_sessions():
    clock = Clock()
    orchestrator = QaOrchestrator(ttl_seconds=60, max_sessions=10, clock=clock)
    started = orchestrator.start("ordinary_review")
    clock.now += timedelta(seconds=40)
    renewed = orchestrator.renew(started.run_id, expected_revision=0)
    assert renewed.expires_at > started.expires_at
    assert renewed.revision == 0
    assert renewed.transition_receipts == []
    with pytest.raises(OrchestrationError, match="stale session revision"):
        orchestrator.renew(started.run_id, expected_revision=1)
    orchestrator.advance(run_id=started.run_id, completed_step="triage", status="partial")
    with pytest.raises(OrchestrationError, match="only active"):
        orchestrator.renew(started.run_id)
    active = orchestrator.start("ordinary_review")
    clock.now += timedelta(seconds=61)
    with pytest.raises(OrchestrationError, match="expired session"):
        orchestrator.renew(active.run_id)


def test_invalid_revision_and_unpaired_retry_keys_are_rejected():
    orchestrator = QaOrchestrator(ttl_seconds=60, max_sessions=10)
    started = orchestrator.start("ordinary_review")
    with pytest.raises(OrchestrationError, match="invalid transition signal"):
        orchestrator.advance(**{**request(started.run_id), "expected_revision": None})
    with pytest.raises(OrchestrationError, match="invalid lease renewal"):
        orchestrator.renew(started.run_id, expected_revision=True)


def test_new_finalization_requires_the_expected_revision():
    orchestrator = QaOrchestrator(ttl_seconds=60, max_sessions=10)
    started = orchestrator.start("ordinary_review")
    stopped = orchestrator.advance(run_id=started.run_id, completed_step="triage", status="partial")
    with pytest.raises(OrchestrationError, match="stale session revision"):
        orchestrator.finish(run_id=started.run_id, outcome="partial", expected_revision=0)
    finished = orchestrator.finish(
        run_id=started.run_id, outcome="partial", expected_revision=stopped.revision
    )
    assert finished.revision == stopped.revision + 1
    assert orchestrator.finish(run_id=started.run_id, outcome="partial") == finished
