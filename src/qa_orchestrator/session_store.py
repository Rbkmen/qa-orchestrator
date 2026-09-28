"""Opt-in, local persistence for content-free orchestration sessions."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

if TYPE_CHECKING:
    from qa_orchestrator.orchestration import QaOrchestrationSession

_SCHEMA_VERSION = 1


class SqliteSessionStore:
    """Persist only structured session state when a local path is configured."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
            raise ValueError("session store path must be a regular file")
        if not self.path.exists():
            try:
                file_descriptor = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                pass
            else:
                os.close(file_descriptor)
        if self.path.is_symlink() or not self.path.is_file():
            raise ValueError("session store path must be a regular file")
        if os.name != "nt":
            self.path.chmod(0o600)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        if self.path.is_symlink() or not self.path.is_file():
            raise ValueError("session store path must be a regular file")
        connection = sqlite3.connect(self.path, timeout=5)
        if os.name != "nt":
            self.path.chmod(0o600)
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS session_store_metadata "
                "(schema_version INTEGER NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS sessions "
                "(run_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )
            row = connection.execute(
                "SELECT schema_version FROM session_store_metadata LIMIT 1"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO session_store_metadata (schema_version) VALUES (?)",
                    (_SCHEMA_VERSION,),
                )
            elif row[0] != _SCHEMA_VERSION:
                raise ValueError("unsupported session store schema version")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def load_all(self) -> list[QaOrchestrationSession]:
        from qa_orchestrator.orchestration import QaOrchestrationSession

        connection = self._connect()
        try:
            rows = connection.execute("SELECT run_id, payload FROM sessions").fetchall()
        except sqlite3.Error as exc:
            raise ValueError("unable to read session store") from exc
        finally:
            connection.close()

        sessions = []
        try:
            for run_id, payload in rows:
                session = QaOrchestrationSession.model_validate_json(payload)
                if session.run_id != run_id:
                    raise ValueError("session store contains a mismatched run id")
                sessions.append(session)
        except (ValidationError, ValueError) as exc:
            raise ValueError("session store contains invalid session data") from exc
        return sessions

    def save(self, session: QaOrchestrationSession) -> None:
        connection = self._connect()
        try:
            connection.execute(
                "INSERT INTO sessions (run_id, payload) VALUES (?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET payload = excluded.payload",
                (session.run_id, session.model_dump_json()),
            )
            connection.commit()
        except sqlite3.Error as exc:
            connection.rollback()
            raise ValueError("unable to write session store") from exc
        finally:
            connection.close()

    def delete(self, run_id: str) -> None:
        connection = self._connect()
        try:
            connection.execute("DELETE FROM sessions WHERE run_id = ?", (run_id,))
            connection.commit()
        except sqlite3.Error as exc:
            connection.rollback()
            raise ValueError("unable to update session store") from exc
        finally:
            connection.close()
