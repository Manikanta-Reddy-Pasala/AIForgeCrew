"""Persistent chat sessions for the Claude-style multi-conversation UI.

SQLite-backed, always available (independent of the ticket/memory
backend). Stores one row per session and one row per message; the
assistant's streamed steps (thoughts + tool calls) are kept as JSON so a
resumed conversation renders exactly as it streamed.

Lives at ``$AIFORGE_CHAT_DB_PATH`` (default ``$AIFORGE_CONFIG_DIR/chat.db``).
On the compose deploy that path is under the persisted ``app_state``
volume, so conversations survive redeploys.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator

_LOCK = threading.Lock()

_DDL = """
CREATE TABLE IF NOT EXISTS chat_sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL DEFAULT 'New chat',
    cwd         TEXT,
    role        TEXT NOT NULL DEFAULT 'doer',
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL DEFAULT '',
    steps       TEXT NOT NULL DEFAULT '[]',
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS chat_messages_session ON chat_messages(session_id, id);
"""

_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"


def _db_path() -> str:
    return os.environ.get(
        "AIFORGE_CHAT_DB_PATH",
        os.path.join(
            os.path.expanduser(os.environ.get("AIFORGE_CONFIG_DIR", "~/.aiforge")),
            "chat.db",
        ),
    )


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    path = _db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    c = sqlite3.connect(path, timeout=30.0)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    try:
        c.executescript(_DDL)
        # Migrate pre-role databases (SQLite has no ADD COLUMN IF NOT EXISTS).
        cols = {r[1] for r in c.execute("PRAGMA table_info(chat_sessions)")}
        if "role" not in cols:
            c.execute("ALTER TABLE chat_sessions ADD COLUMN role TEXT "
                      "NOT NULL DEFAULT 'doer'")
        yield c
        c.commit()
    finally:
        c.close()


def _iso(v):
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).isoformat()
        except ValueError:
            return v
    return v


def _session_row(r: sqlite3.Row) -> dict:
    keys = r.keys()
    return {"id": r["id"], "title": r["title"], "cwd": r["cwd"],
            "role": (r["role"] if "role" in keys else "doer") or "doer",
            "created_at": _iso(r["created_at"]), "updated_at": _iso(r["updated_at"])}


def create_session(title: str = "New chat", cwd: str | None = None,
                   role: str = "doer") -> dict:
    with _LOCK, _conn() as c:
        cur = c.execute(
            "INSERT INTO chat_sessions(title, cwd, role) VALUES (?,?,?)",
            (title or "New chat", cwd, role or "doer"),
        )
        r = c.execute("SELECT * FROM chat_sessions WHERE id=?",
                      (cur.lastrowid,)).fetchone()
    return _session_row(r)


def set_session_role(session_id: int, role: str) -> "dict | None":
    with _conn() as c:
        c.execute(f"UPDATE chat_sessions SET role=?, updated_at={_NOW} WHERE id=?",
                  (role or "doer", session_id))
        r = c.execute("SELECT * FROM chat_sessions WHERE id=?",
                      (session_id,)).fetchone()
    return _session_row(r) if r else None


def list_sessions() -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT s.*, "
            "(SELECT COUNT(*) FROM chat_messages m WHERE m.session_id=s.id) AS n "
            "FROM chat_sessions s ORDER BY s.updated_at DESC, s.id DESC"
        ).fetchall()
    out = []
    for r in rows:
        d = _session_row(r)
        d["message_count"] = r["n"]
        out.append(d)
    return out


def get_session(session_id: int) -> "dict | None":
    with _conn() as c:
        r = c.execute("SELECT * FROM chat_sessions WHERE id=?",
                      (session_id,)).fetchone()
    return _session_row(r) if r else None


def get_messages(session_id: int) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id, role, content, steps, created_at FROM chat_messages "
            "WHERE session_id=? ORDER BY id ASC", (session_id,),
        ).fetchall()
    out = []
    for r in rows:
        try:
            steps = json.loads(r["steps"] or "[]")
        except (ValueError, TypeError):
            steps = []
        out.append({"id": r["id"], "role": r["role"], "content": r["content"],
                    "steps": steps, "created_at": _iso(r["created_at"])})
    return out


def add_message(session_id: int, role: str, content: str,
                steps: "list | None" = None) -> int:
    with _LOCK, _conn() as c:
        cur = c.execute(
            "INSERT INTO chat_messages(session_id, role, content, steps) "
            "VALUES (?,?,?,?)",
            (session_id, role, content, json.dumps(steps or [])),
        )
        c.execute(f"UPDATE chat_sessions SET updated_at={_NOW} WHERE id=?",
                  (session_id,))
        return int(cur.lastrowid)


def rename_session(session_id: int, title: str) -> "dict | None":
    with _conn() as c:
        c.execute(f"UPDATE chat_sessions SET title=?, updated_at={_NOW} WHERE id=?",
                  (title.strip() or "New chat", session_id))
        r = c.execute("SELECT * FROM chat_sessions WHERE id=?",
                      (session_id,)).fetchone()
    return _session_row(r) if r else None


def delete_session(session_id: int) -> bool:
    with _conn() as c:
        cur = c.execute("DELETE FROM chat_sessions WHERE id=?", (session_id,))
    return cur.rowcount > 0
