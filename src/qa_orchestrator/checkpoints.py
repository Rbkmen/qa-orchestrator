"""Opt-in local checkpoints of validated profile results, keyed by the full input."""

import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from qa_orchestrator.contracts import ProfileReviewReport


def checkpoint_key(context, route, model_policy, previous_reports, executor_identity: str) -> str:
    if not executor_identity.strip():
        raise ValueError("checkpoint reuse requires an explicit executor/instruction version")
    data = {
        "schema_version": 1,
        "context": context.model_dump(mode="json"),
        "route": route.model_dump(mode="json"),
        "model_policy": model_policy.model_dump(mode="json") if model_policy else None,
        "previous_reports": [r.model_dump(mode="json") for r in previous_reports],
        "executor_identity": executor_identity,
    }
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class ProfileCheckpointStore:
    """One SQLite file, private on POSIX; bounded rows and insert-only results."""

    def __init__(self, path: Path, *, max_entries: int = 1000):
        if not 1 <= max_entries <= 10000:
            raise ValueError("checkpoint limit must be between 1 and 10000")
        self.path, self.max_entries = Path(path).expanduser(), max_entries
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._check_path()
        if not self.path.exists():
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(descriptor)
        self._check_path()
        if os.name != "nt":
            self.path.chmod(0o600)
        with closing(self._connect()) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS profile_checkpoints "
                "(key TEXT PRIMARY KEY, payload TEXT NOT NULL, digest TEXT NOT NULL, "
                "created INTEGER NOT NULL DEFAULT (unixepoch()))"
            )

    def _check_path(self):
        if any(p.is_symlink() for p in (self.path, *self.path.parents)):
            raise ValueError("checkpoint path must not contain symlinks")
        if self.path.exists() and not self.path.is_file():
            raise ValueError("checkpoint store must be a regular file")

    def _connect(self):
        self._check_path()
        return sqlite3.connect(self.path, timeout=5)

    def load(self, key: str) -> ProfileReviewReport | None:
        self._validate_key(key)
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT payload, digest FROM profile_checkpoints WHERE key=?", (key,)
            ).fetchone()
        if row is None:
            return None
        if len(row[0].encode()) > 65536 or hashlib.sha256(row[0].encode()).hexdigest() != row[1]:
            raise ValueError("checkpoint content hash mismatch")
        return ProfileReviewReport.model_validate_json(row[0])

    def save(self, key: str, report: ProfileReviewReport):
        self._validate_key(key)
        payload = ProfileReviewReport.model_validate(report).model_dump_json()
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with closing(self._connect()) as db, db:
            db.execute(
                "INSERT OR IGNORE INTO profile_checkpoints(key,payload,digest) VALUES (?,?,?)",
                (key, payload, digest),
            )
            db.execute(
                "DELETE FROM profile_checkpoints WHERE key IN "
                "(SELECT key FROM profile_checkpoints ORDER BY created DESC, rowid DESC LIMIT -1 OFFSET ?)",
                (self.max_entries,),
            )

    def list_keys(self) -> tuple[str, ...]:
        with closing(self._connect()) as db:
            return tuple(
                row[0] for row in db.execute("SELECT key FROM profile_checkpoints ORDER BY key")
            )

    @staticmethod
    def _validate_key(key):
        if re.fullmatch(r"[a-f0-9]{64}", key) is None:
            raise ValueError("invalid checkpoint identity")
