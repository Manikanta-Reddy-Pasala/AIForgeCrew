"""SQLite backend for the chat store (the ``--lite`` embedded default)."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

from ._helpers import _LOCK, _media_out, _message_out, _session_out
from aiforge_core.config.paths import config_dir

_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS chat_sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL DEFAULT 'New chat',
    cwd         TEXT,
    role        TEXT NOT NULL DEFAULT 'doer',
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     INTEGER NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role           TEXT NOT NULL,
    content        TEXT NOT NULL DEFAULT '',
    steps          TEXT NOT NULL DEFAULT '[]',
    checkpoint_sha TEXT,
    mode           TEXT NOT NULL DEFAULT 'simple',
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS chat_messages_session ON chat_messages(session_id, id);
CREATE TABLE IF NOT EXISTS chat_media (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    filename    TEXT NOT NULL,
    path        TEXT NOT NULL,
    mime        TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS chat_media_session ON chat_media(session_id, id);
"""

_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"


def _add_column_if_missing(c: "sqlite3.Connection", table: str, column: str,
                           decl: str) -> None:
    """Idempotent ``ALTER TABLE ADD COLUMN`` — SQLite has no ADD COLUMN IF NOT
    EXISTS. Guarded by a table_info check AND a swallowed duplicate-column
    error: two connections opened concurrently (the API TestClient runs the app
    on a threadpool) can both read the pre-migration schema and both issue the
    ALTER, and the loser must not abort with "duplicate column name"."""
    cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
    if column in cols:
        return
    try:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    except sqlite3.OperationalError as exc:
        if "duplicate column name" not in str(exc).lower():
            raise


class _SqliteChatStore:
    name = "sqlite"

    def _db_path(self) -> str:
        return os.environ.get(
            "AIFORGE_CHAT_DB_PATH",
            os.path.join(
                str(config_dir()),
                "chat.db",
            ),
        )

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        path = self._db_path()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        c = sqlite3.connect(path, timeout=30.0)
        c.row_factory = sqlite3.Row
        # busy_timeout FIRST: the connect `timeout` sets a busy handler for
        # statement execution, but switching journal mode needs a brief
        # exclusive lock that under concurrent connections (the API TestClient
        # runs the app on a threadpool) otherwise fails IMMEDIATELY with
        # "database is locked" instead of waiting. An explicit busy_timeout makes
        # the WAL switch (and every later statement) wait for the lock. This
        # surfaced as a roaming failure in test_chat_resume_route.
        c.execute("PRAGMA busy_timeout=30000")
        # journal_mode=WAL persists in the DB file, so a concurrent reader can
        # leave it already-WAL and the switch is a no-op; tolerate a transient
        # lock here rather than fail the whole request.
        try:
            c.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
        c.execute("PRAGMA foreign_keys=ON")
        try:
            c.executescript(_SQLITE_DDL)
            # Migrate pre-role databases (SQLite has no ADD COLUMN IF NOT EXISTS).
            _add_column_if_missing(c, "chat_sessions", "role",
                                   "TEXT NOT NULL DEFAULT 'doer'")
            # Migrate pre-checkpoint message tables (edit-resend / restore-to-turn).
            _add_column_if_missing(c, "chat_messages", "checkpoint_sha", "TEXT")
            # Per-turn run mode (simple|plan|team) — so the UI can badge which
            # mode each turn/session ran in (was composer-only, never persisted).
            _add_column_if_missing(c, "chat_messages", "mode",
                                   "TEXT NOT NULL DEFAULT 'simple'")
            # Per-turn wall-clock seconds — so every turn (simple/plan/team)
            # shows its time-taken even after reload (client timer is live-only).
            _add_column_if_missing(c, "chat_messages", "duration_s", "REAL")
            yield c
            c.commit()
        finally:
            c.close()

    def create_session(self, title="New chat", cwd=None, role="chat") -> dict:
        with _LOCK, self._conn() as c:
            cur = c.execute(
                "INSERT INTO chat_sessions(title, cwd, role) VALUES (?,?,?)",
                (title or "New chat", cwd, role or "doer"),
            )
            r = c.execute("SELECT * FROM chat_sessions WHERE id=?",
                          (cur.lastrowid,)).fetchone()
        return _session_out(dict(r))

    def set_session_cwd(self, session_id, cwd):
        with self._conn() as c:
            c.execute(f"UPDATE chat_sessions SET cwd=?, updated_at={_NOW} WHERE id=?",
                      (cwd, session_id))
            r = c.execute("SELECT * FROM chat_sessions WHERE id=?",
                          (session_id,)).fetchone()
        return _session_out(dict(r)) if r else None

    def set_session_role(self, session_id, role):
        with self._conn() as c:
            c.execute(f"UPDATE chat_sessions SET role=?, updated_at={_NOW} WHERE id=?",
                      (role or "doer", session_id))
            r = c.execute("SELECT * FROM chat_sessions WHERE id=?",
                          (session_id,)).fetchone()
        return _session_out(dict(r)) if r else None

    def list_sessions(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT s.*, "
                "(SELECT COUNT(*) FROM chat_messages m WHERE m.session_id=s.id) AS n, "
                "(SELECT m.mode FROM chat_messages m WHERE m.session_id=s.id "
                " AND m.role='user' ORDER BY m.id DESC LIMIT 1) AS last_mode "
                "FROM chat_sessions s ORDER BY s.updated_at DESC, s.id DESC"
            ).fetchall()
        out = []
        for r in rows:
            d = _session_out(dict(r))
            d["message_count"] = r["n"]
            d["last_mode"] = (r["last_mode"] or "simple")
            out.append(d)
        return out

    def get_session(self, session_id):
        with self._conn() as c:
            r = c.execute("SELECT * FROM chat_sessions WHERE id=?",
                          (session_id,)).fetchone()
        return _session_out(dict(r)) if r else None

    def get_messages(self, session_id) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, role, content, steps, checkpoint_sha, mode, "
                "duration_s, created_at "
                "FROM chat_messages WHERE session_id=? ORDER BY id ASC",
                (session_id,),
            ).fetchall()
        return [_message_out(dict(r)) for r in rows]

    def _search_candidates(self, toks, exclude_session) -> list[dict]:
        if not toks:                       # empty tokens → invalid SQL (WHERE ())
            return []
        where = " OR ".join(["instr(lower(m.content), ?)"] * len(toks))
        params: list = list(toks)
        sql = (
            "SELECT m.session_id AS session_id, m.role AS role, "
            "m.content AS content, m.created_at AS created_at, m.id AS id, "
            "COALESCE(s.title, '') AS session_title "
            "FROM chat_messages m LEFT JOIN chat_sessions s ON s.id = m.session_id "
            f"WHERE ({where}) AND TRIM(m.content) <> ''"
        )
        if exclude_session is not None:
            sql += " AND m.session_id <> ?"
            params.append(exclude_session)
        sql += " ORDER BY m.id DESC LIMIT 500"
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql, params).fetchall()]

    def add_message(self, session_id, role, content, steps=None,
                    mode="simple", duration_s=None) -> int:
        with _LOCK, self._conn() as c:
            cur = c.execute(
                "INSERT INTO chat_messages(session_id, role, content, steps, "
                "mode, duration_s) VALUES (?,?,?,?,?,?)",
                (session_id, role, content, json.dumps(steps or []),
                 mode or "simple", duration_s),
            )
            c.execute(f"UPDATE chat_sessions SET updated_at={_NOW} WHERE id=?",
                      (session_id,))
            return int(cur.lastrowid)

    def set_message_checkpoint(self, message_id, sha) -> None:
        with self._conn() as c:
            c.execute("UPDATE chat_messages SET checkpoint_sha=? WHERE id=?",
                      (sha or None, message_id))

    def delete_messages_from(self, session_id, message_id) -> int:
        with _LOCK, self._conn() as c:
            cur = c.execute(
                "DELETE FROM chat_messages WHERE session_id=? AND id>=?",
                (session_id, message_id),
            )
            c.execute(f"UPDATE chat_sessions SET updated_at={_NOW} WHERE id=?",
                      (session_id,))
            return cur.rowcount

    def message_checkpoint(self, session_id, message_id):
        with self._conn() as c:
            r = c.execute(
                "SELECT checkpoint_sha FROM chat_messages WHERE id=? AND session_id=?",
                (message_id, session_id),
            ).fetchone()
        return (r["checkpoint_sha"] if r else None) or None

    def rename_session(self, session_id, title):
        with self._conn() as c:
            c.execute(f"UPDATE chat_sessions SET title=?, updated_at={_NOW} WHERE id=?",
                      (title.strip() or "New chat", session_id))
            r = c.execute("SELECT * FROM chat_sessions WHERE id=?",
                          (session_id,)).fetchone()
        return _session_out(dict(r)) if r else None

    def delete_session(self, session_id) -> bool:
        with self._conn() as c:
            c.execute("DELETE FROM chat_media WHERE session_id=?", (session_id,))
            c.execute("DELETE FROM chat_messages WHERE session_id=?", (session_id,))
            cur = c.execute("DELETE FROM chat_sessions WHERE id=?", (session_id,))
        return cur.rowcount > 0

    def delete_all_sessions(self) -> int:
        with self._conn() as c:
            n = c.execute("SELECT COUNT(*) FROM chat_sessions").fetchone()[0]
            c.execute("DELETE FROM chat_media")
            c.execute("DELETE FROM chat_messages")
            c.execute("DELETE FROM chat_sessions")
            c.execute("DELETE FROM sqlite_sequence WHERE name IN "
                      "('chat_sessions', 'chat_messages', 'chat_media')")
        return int(n or 0)

    def add_media(self, session_id, filename, path, mime="", description="") -> dict:
        with _LOCK, self._conn() as c:
            cur = c.execute(
                "INSERT INTO chat_media(session_id, filename, path, mime, description) "
                "VALUES (?,?,?,?,?)",
                (session_id, filename, path, mime or "", description or ""),
            )
            c.execute(f"UPDATE chat_sessions SET updated_at={_NOW} WHERE id=?",
                      (session_id,))
            r = c.execute("SELECT * FROM chat_media WHERE id=?",
                          (cur.lastrowid,)).fetchone()
        return _media_out(dict(r))

    def list_media(self, session_id) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM chat_media WHERE session_id=? ORDER BY id",
                (session_id,)).fetchall()
        return [_media_out(dict(r)) for r in rows]

    def get_media(self, media_id):
        with self._conn() as c:
            r = c.execute("SELECT * FROM chat_media WHERE id=?",
                          (media_id,)).fetchone()
        return _media_out(dict(r)) if r else None

    def set_media_description(self, media_id, description):
        with self._conn() as c:
            c.execute("UPDATE chat_media SET description=? WHERE id=?",
                      (description or "", media_id))
            r = c.execute("SELECT * FROM chat_media WHERE id=?",
                          (media_id,)).fetchone()
        return _media_out(dict(r)) if r else None

    def delete_media(self, media_id):
        with self._conn() as c:
            r = c.execute("SELECT * FROM chat_media WHERE id=?",
                          (media_id,)).fetchone()
            if r is None:
                return None
            c.execute("DELETE FROM chat_media WHERE id=?", (media_id,))
        return _media_out(dict(r))
