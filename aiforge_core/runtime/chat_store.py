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
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator

_LOCK = threading.Lock()

# Very small stopword set — dropped so a query like "how do we handle X" keys
# off the meaningful terms (X) rather than matching every message with "how".
_STOPWORDS = frozenset({
    "the", "and", "for", "are", "was", "were", "you", "your", "our", "with",
    "how", "what", "why", "when", "who", "where", "should", "would", "could",
    "can", "does", "did", "this", "that", "these", "those", "from", "have",
    "has", "had", "will", "not", "but", "all", "any", "use", "used", "using",
})


def _tokens(query: str) -> list[str]:
    """Lowercase alphanumeric tokens, len>=3, minus common stopwords."""
    if not query:
        return []
    raw = re.split(r"[^a-z0-9]+", query.lower())
    seen: dict[str, None] = {}
    for t in raw:
        if len(t) >= 3 and t not in _STOPWORDS:
            seen[t] = None
    return list(seen)

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
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     INTEGER NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role           TEXT NOT NULL,
    content        TEXT NOT NULL DEFAULT '',
    steps          TEXT NOT NULL DEFAULT '[]',
    checkpoint_sha TEXT,
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
        # Migrate pre-checkpoint message tables (edit-resend / restore-to-turn).
        mcols = {r[1] for r in c.execute("PRAGMA table_info(chat_messages)")}
        if "checkpoint_sha" not in mcols:
            c.execute("ALTER TABLE chat_messages ADD COLUMN checkpoint_sha TEXT")
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
                   role: str = "chat") -> dict:
    with _LOCK, _conn() as c:
        cur = c.execute(
            "INSERT INTO chat_sessions(title, cwd, role) VALUES (?,?,?)",
            (title or "New chat", cwd, role or "doer"),
        )
        r = c.execute("SELECT * FROM chat_sessions WHERE id=?",
                      (cur.lastrowid,)).fetchone()
    return _session_row(r)


def set_session_cwd(session_id: int, cwd: str) -> "dict | None":
    with _conn() as c:
        c.execute(f"UPDATE chat_sessions SET cwd=?, updated_at={_NOW} WHERE id=?",
                  (cwd, session_id))
        r = c.execute("SELECT * FROM chat_sessions WHERE id=?",
                      (session_id,)).fetchone()
    return _session_row(r) if r else None


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
            "SELECT id, role, content, steps, checkpoint_sha, created_at "
            "FROM chat_messages WHERE session_id=? ORDER BY id ASC",
            (session_id,),
        ).fetchall()
    out = []
    for r in rows:
        try:
            steps = json.loads(r["steps"] or "[]")
        except (ValueError, TypeError):
            steps = []
        keys = r.keys()
        out.append({"id": r["id"], "role": r["role"], "content": r["content"],
                    "steps": steps,
                    "checkpoint_sha": (r["checkpoint_sha"]
                                       if "checkpoint_sha" in keys else None),
                    "created_at": _iso(r["created_at"])})
    return out


def search_messages(query: str, *, limit: int = 6,
                    exclude_session: "int | None" = None) -> list[dict]:
    """Full-text-ish search over prior chat message CONTENT (all sessions except
    ``exclude_session``). Cheap + local: one indexed-ish LIKE scan over SQLite,
    no LLM, no network — the whole point of making prior chats searchable.

    A message matches if its content contains ANY query token (tokens: lowercase
    alphanumeric, len>=3, common stopwords dropped). Ranked by (# distinct tokens
    matched desc, then recency desc). Returns up to ``limit`` hits:
    ``[{"session_id","session_title","role","content","created_at"}]`` with each
    content truncated to ~300 chars. Soft-fail: any error → []."""
    toks = _tokens(query)
    if not toks:
        return []
    try:
        # ANY-token prefilter in SQL (keeps the scan to candidate rows); the
        # exact per-token overlap count + ranking is done in Python (portable,
        # avoids brittle dynamic scoring SQL).
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
        # Cap the candidate set (recency-first) so a huge chat DB stays cheap.
        sql += " ORDER BY m.id DESC LIMIT 500"
        with _conn() as c:
            rows = c.execute(sql, params).fetchall()
    except Exception:  # noqa: BLE001 — search must never break a chat turn
        return []
    scored: list[tuple] = []
    for r in rows:
        content = (r["content"] or "").strip()
        if not content:
            continue
        low = content.lower()
        matched = sum(1 for t in toks if t in low)
        if matched == 0:
            continue
        # rank key: more distinct tokens first, then newer (higher id) first.
        scored.append((matched, r["id"], r, content))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    out: list[dict] = []
    for _matched, _id, r, content in scored[:max(0, int(limit or 0))]:
        out.append({
            "session_id": r["session_id"],
            "session_title": r["session_title"] or "Untitled chat",
            "role": r["role"],
            "content": content[:300],
            "created_at": _iso(r["created_at"]),
        })
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


def set_message_checkpoint(message_id: int, sha: str) -> None:
    """Stamp the workspace checkpoint sha taken just before this message — so
    edit-resend can restore the tree to exactly that turn's state."""
    with _conn() as c:
        c.execute("UPDATE chat_messages SET checkpoint_sha=? WHERE id=?",
                  (sha or None, message_id))


def delete_messages_from(session_id: int, message_id: int) -> int:
    """Delete this message and every message after it in the session (by the
    stable autoincrement id ordering). Returns the number of rows removed.
    Used by edit-and-resend: truncate history at the edited turn before re-running."""
    with _LOCK, _conn() as c:
        cur = c.execute(
            "DELETE FROM chat_messages WHERE session_id=? AND id>=?",
            (session_id, message_id),
        )
        c.execute(f"UPDATE chat_sessions SET updated_at={_NOW} WHERE id=?",
                  (session_id,))
        return cur.rowcount


def message_checkpoint(session_id: int, message_id: int) -> "str | None":
    """The checkpoint sha stamped on a given message, or None."""
    with _conn() as c:
        r = c.execute(
            "SELECT checkpoint_sha FROM chat_messages WHERE id=? AND session_id=?",
            (message_id, session_id),
        ).fetchone()
    return (r["checkpoint_sha"] if r else None) or None


def rename_session(session_id: int, title: str) -> "dict | None":
    with _conn() as c:
        c.execute(f"UPDATE chat_sessions SET title=?, updated_at={_NOW} WHERE id=?",
                  (title.strip() or "New chat", session_id))
        r = c.execute("SELECT * FROM chat_sessions WHERE id=?",
                      (session_id,)).fetchone()
    return _session_row(r) if r else None


def delete_session(session_id: int) -> bool:
    with _conn() as c:
        c.execute("DELETE FROM chat_media WHERE session_id=?", (session_id,))
        c.execute("DELETE FROM chat_messages WHERE session_id=?", (session_id,))
        cur = c.execute("DELETE FROM chat_sessions WHERE id=?", (session_id,))
    return cur.rowcount > 0


def delete_all_sessions() -> int:
    """Delete EVERY chat session + its messages and reset the id autoincrement
    so new sessions start at 1. Returns the count of sessions deleted."""
    with _conn() as c:
        n = c.execute("SELECT COUNT(*) FROM chat_sessions").fetchone()[0]
        c.execute("DELETE FROM chat_media")
        c.execute("DELETE FROM chat_messages")
        c.execute("DELETE FROM chat_sessions")
        c.execute("DELETE FROM sqlite_sequence WHERE name IN "
                  "('chat_sessions', 'chat_messages', 'chat_media')")
    return int(n or 0)


# ── Chat media (uploaded images + their descriptions) ─────────────────────────

def _media_row(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "session_id": r["session_id"],
            "filename": r["filename"], "path": r["path"],
            "mime": r["mime"], "description": r["description"],
            "created_at": _iso(r["created_at"])}


def add_media(session_id: int, filename: str, path: str,
              mime: str = "", description: str = "") -> dict:
    with _LOCK, _conn() as c:
        cur = c.execute(
            "INSERT INTO chat_media(session_id, filename, path, mime, description) "
            "VALUES (?,?,?,?,?)",
            (session_id, filename, path, mime or "", description or ""),
        )
        c.execute(f"UPDATE chat_sessions SET updated_at={_NOW} WHERE id=?",
                  (session_id,))
        r = c.execute("SELECT * FROM chat_media WHERE id=?",
                      (cur.lastrowid,)).fetchone()
    return _media_row(r)


def list_media(session_id: int) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM chat_media WHERE session_id=? ORDER BY id",
            (session_id,)).fetchall()
    return [_media_row(r) for r in rows]


def get_media(media_id: int) -> "dict | None":
    with _conn() as c:
        r = c.execute("SELECT * FROM chat_media WHERE id=?",
                      (media_id,)).fetchone()
    return _media_row(r) if r else None


def set_media_description(media_id: int, description: str) -> "dict | None":
    with _conn() as c:
        c.execute("UPDATE chat_media SET description=? WHERE id=?",
                  (description or "", media_id))
        r = c.execute("SELECT * FROM chat_media WHERE id=?",
                      (media_id,)).fetchone()
    return _media_row(r) if r else None


def delete_media(media_id: int) -> "dict | None":
    """Delete the row, returning it (so the caller can unlink the file)."""
    with _conn() as c:
        r = c.execute("SELECT * FROM chat_media WHERE id=?",
                      (media_id,)).fetchone()
        if r is None:
            return None
        c.execute("DELETE FROM chat_media WHERE id=?", (media_id,))
    return _media_row(r)
