"""Opt-in, local persistence for content-free orchestration sessions."""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

if TYPE_CHECKING:
    from qa_orchestrator.orchestration import QaOrchestrationSession

_SCHEMA_VERSION = 1


def inspect_session_store(path: Path) -> str | None:
    """Read and validate a configured store without creating or changing files.

    Return ``None`` when the store has not been initialized yet.
    """

    path = Path(path).expanduser()
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError("unable to access session store") from exc

    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("session store path must be a regular file")
    if os.name != "nt" and metadata.st_mode & 0o077:
        raise ValueError("session store file permissions must be private")

    try:
        uri = path.resolve(strict=True).as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, timeout=5, uri=True)
    except (OSError, sqlite3.Error) as exc:
        raise ValueError("unable to read session store") from exc

    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            raise ValueError("session store integrity check failed")

        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if not {"session_store_metadata", "sessions"}.issubset(tables):
            raise ValueError("unsupported session store schema")

        metadata_rows = connection.execute(
            "SELECT schema_version FROM session_store_metadata"
        ).fetchall()
        if len(metadata_rows) != 1 or metadata_rows[0][0] != _SCHEMA_VERSION:
            raise ValueError("unsupported session store schema")

        rows = connection.execute("SELECT run_id, payload FROM sessions").fetchall()
    except ValueError:
        raise
    except sqlite3.Error as exc:
        raise ValueError("unable to read session store") from exc
    finally:
        connection.close()

    from qa_orchestrator.orchestration import QaOrchestrationSession

    try:
        for run_id, payload in rows:
            session = QaOrchestrationSession.model_validate_json(payload)
            if session.run_id != run_id:
                raise ValueError("session store contains a mismatched run id")
    except (ValidationError, ValueError) as exc:
        raise ValueError("session store contains invalid session data") from exc

    return "integrity, schema, and persisted sessions are valid"


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
        self.delete_many((run_id,))

    def delete_many(self, run_ids: Sequence[str]) -> None:
        """Delete expired session rows in one transaction."""

        unique_run_ids = tuple(dict.fromkeys(run_ids))
        if not unique_run_ids:
            return

        connection = self._connect()
        try:
            connection.executemany(
                "DELETE FROM sessions WHERE run_id = ?",
                ((run_id,) for run_id in unique_run_ids),
            )
            connection.commit()
        except sqlite3.Error as exc:
            connection.rollback()
            raise ValueError("unable to update session store") from exc
        finally:
            connection.close()
