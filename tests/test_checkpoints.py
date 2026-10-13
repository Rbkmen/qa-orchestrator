import os
import sqlite3

import pytest

from qa_orchestrator.checkpoints import ProfileCheckpointStore, checkpoint_key
from qa_orchestrator.contracts import ProfileReviewReport
from qa_orchestrator.review_profiles import build_review_route


def test_every_review_input_changes_checkpoint_identity(bound_review_context):
    route = build_review_route("python_reviewer")
    key = checkpoint_key(bound_review_context, route, None, (), "v1")
    for field, value in (
        ("head_sha", "a" * 40),
        ("diff_sha256", "b" * 64),
        ("evidence_packet_sha256", "c" * 64),
        ("repository", "example/new"),
    ):
        changed = bound_review_context.model_copy(update={field: value})
        assert checkpoint_key(changed, route, None, (), "v1") != key
    assert checkpoint_key(bound_review_context, route, None, (), "v2") != key
    assert (
        checkpoint_key(
            bound_review_context,
            route,
            None,
            (ProfileReviewReport(profile="code_explorer", scope="Different input"),),
            "v1",
        )
        != key
    )


def test_checkpoints_survive_restart_detect_corruption_and_bound_storage(tmp_path):
    path = tmp_path / "profiles.sqlite"
    store = ProfileCheckpointStore(path, max_entries=1)
    report = ProfileReviewReport(profile="python_reviewer", scope="Inspected")
    store.save("a" * 64, report)
    assert ProfileCheckpointStore(path).load("a" * 64) == report
    store.save("b" * 64, report)
    assert store.load("a" * 64) is None
    with sqlite3.connect(path) as db:
        db.execute("UPDATE profile_checkpoints SET payload='{}'")
    with pytest.raises(ValueError, match="hash mismatch"):
        store.load("b" * 64)
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0
