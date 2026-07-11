"""Persistent chat sessions for the Claude-style multi-conversation UI.

Backend-neutral: the SAME public function API routes to a SQLite impl
(embedded, the ``--lite`` default) OR a Postgres impl, chosen ONCE per process
by ``AIFORGE_PG_URL`` (same switch as the tickets store). The public functions
(create_session, add_message, get_messages, search_messages, …) and their
return shapes are identical across backends, so every caller (api.py,
chat_agent.py, chat_summary.py, chat_media.py) is unchanged.

SQLite path lives at ``$AIFORGE_CHAT_DB_PATH`` (default
``$AIFORGE_CONFIG_DIR/chat.db``). On the compose deploy the chat tables live
in the shared Postgres so conversations survive redeploys alongside tickets.
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

from aiforge_core.config import env as _env

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


def _iso(v):
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).isoformat()
        except ValueError:
            return v
    if isinstance(v, datetime):
        return v.isoformat()
    return v


# ── shared row normalizers (operate on plain dicts) ───────────────────────────

def _session_out(d: dict) -> dict:
    return {"id": d["id"], "title": d["title"], "cwd": d["cwd"],
            "role": (d.get("role") or "doer"),
            "created_at": _iso(d["created_at"]),
            "updated_at": _iso(d["updated_at"])}


def _message_out(d: dict) -> dict:
    try:
        steps = json.loads(d.get("steps") or "[]")
    except (ValueError, TypeError):
        steps = []
    return {"id": d["id"], "role": d["role"], "content": d["content"],
            "steps": steps, "checkpoint_sha": d.get("checkpoint_sha"),
            "mode": (d.get("mode") or "simple"),
            "duration_s": d.get("duration_s"),
            "created_at": _iso(d["created_at"])}


def _media_out(d: dict) -> dict:
    return {"id": d["id"], "session_id": d["session_id"],
            "filename": d["filename"], "path": d["path"],
            "mime": d["mime"], "description": d["description"],
            "created_at": _iso(d["created_at"])}


def _rank_search(rows: list[dict], toks: list[str], limit: int) -> list[dict]:
    """Shared ranking for search_messages — portable Python over candidate rows
    (keys: session_id, session_title, role, content, created_at, id)."""
    scored: list[tuple] = []
    for r in rows:
        content = (r.get("content") or "").strip()
        if not content:
            continue
        low = content.lower()
        matched = sum(1 for t in toks if t in low)
        if matched == 0:
            continue
        scored.append((matched, r["id"], r, content))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    out: list[dict] = []
    for _matched, _id, r, content in scored[:max(0, int(limit or 0))]:
        out.append({
            "session_id": r["session_id"],
            "session_title": r.get("session_title") or "Untitled chat",
            "role": r["role"],
            "content": content[:300],
            "created_at": _iso(r["created_at"]),
        })
    return out


# ══════════════════════════════ SQLite backend ══════════════════════════════

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


class _SqliteChatStore:
    name = "sqlite"

    def _db_path(self) -> str:
        return os.environ.get(
            "AIFORGE_CHAT_DB_PATH",
            os.path.join(
                os.path.expanduser(os.environ.get("AIFORGE_CONFIG_DIR", "~/.aiforge")),
                "chat.db",
            ),
        )

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        path = self._db_path()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        c = sqlite3.connect(path, timeout=30.0)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        try:
            c.executescript(_SQLITE_DDL)
            # Migrate pre-role databases (SQLite has no ADD COLUMN IF NOT EXISTS).
            cols = {r[1] for r in c.execute("PRAGMA table_info(chat_sessions)")}
            if "role" not in cols:
                c.execute("ALTER TABLE chat_sessions ADD COLUMN role TEXT "
                          "NOT NULL DEFAULT 'doer'")
            # Migrate pre-checkpoint message tables (edit-resend / restore-to-turn).
            mcols = {r[1] for r in c.execute("PRAGMA table_info(chat_messages)")}
            if "checkpoint_sha" not in mcols:
                c.execute("ALTER TABLE chat_messages ADD COLUMN checkpoint_sha TEXT")
            # Per-turn run mode (simple|plan|team) — so the UI can badge which
            # mode each turn/session ran in (was composer-only, never persisted).
            if "mode" not in mcols:
                c.execute("ALTER TABLE chat_messages ADD COLUMN mode TEXT "
                          "NOT NULL DEFAULT 'simple'")
            # Per-turn wall-clock seconds — so every turn (simple/plan/team)
            # shows its time-taken even after reload (client timer is live-only).
            if "duration_s" not in mcols:
                c.execute("ALTER TABLE chat_messages ADD COLUMN duration_s REAL")
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


# ══════════════════════════════ Postgres backend ═════════════════════════════

_PG_DDL = """
CREATE TABLE IF NOT EXISTS chat_sessions (
    id          bigserial PRIMARY KEY,
    title       text NOT NULL DEFAULT 'New chat',
    cwd         text,
    role        text NOT NULL DEFAULT 'doer',
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id             bigserial PRIMARY KEY,
    session_id     bigint NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role           text NOT NULL,
    content        text NOT NULL DEFAULT '',
    steps          text NOT NULL DEFAULT '[]',
    checkpoint_sha text,
    mode           text NOT NULL DEFAULT 'simple',
    duration_s     real,
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chat_messages_session ON chat_messages(session_id, id);
-- migrate pre-existing tables (CREATE IF NOT EXISTS won't add the column)
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS duration_s real;
CREATE TABLE IF NOT EXISTS chat_media (
    id          bigserial PRIMARY KEY,
    session_id  bigint NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    filename    text NOT NULL,
    path        text NOT NULL,
    mime        text NOT NULL DEFAULT '',
    description text NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chat_media_session ON chat_media(session_id, id);
"""


class _PgChatStore:
    name = "postgres"

    def __init__(self, dsn: str):
        self.dsn = dsn
        self._schema_ready = False
        self._schema_lock = threading.Lock()

    @contextmanager
    def _conn(self):
        import psycopg
        c = psycopg.connect(self.dsn, autocommit=False, connect_timeout=5,
                            options="-c statement_timeout=15000")
        try:
            self._ensure_schema(c)
            yield c
        finally:
            c.close()

    def _ensure_schema(self, c) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            try:
                with c.cursor() as cur:
                    cur.execute(_PG_DDL)
                    # Per-turn run mode — idempotent add for pre-existing tables.
                    cur.execute("ALTER TABLE chat_messages ADD COLUMN IF NOT "
                                "EXISTS mode text NOT NULL DEFAULT 'simple'")
                c.commit()
            except Exception:
                c.rollback()
            self._schema_ready = True

    def _cur(self, c):
        from psycopg.rows import dict_row
        return c.cursor(row_factory=dict_row)

    def create_session(self, title="New chat", cwd=None, role="chat") -> dict:
        with self._conn() as c, self._cur(c) as cur:
            cur.execute(
                "INSERT INTO chat_sessions(title, cwd, role) VALUES (%s,%s,%s) "
                "RETURNING *",
                (title or "New chat", cwd, role or "doer"))
            r = cur.fetchone()
            c.commit()
        return _session_out(r)

    def set_session_cwd(self, session_id, cwd):
        with self._conn() as c, self._cur(c) as cur:
            cur.execute("UPDATE chat_sessions SET cwd=%s, updated_at=now() "
                        "WHERE id=%s RETURNING *", (cwd, session_id))
            r = cur.fetchone()
            c.commit()
        return _session_out(r) if r else None

    def set_session_role(self, session_id, role):
        with self._conn() as c, self._cur(c) as cur:
            cur.execute("UPDATE chat_sessions SET role=%s, updated_at=now() "
                        "WHERE id=%s RETURNING *", (role or "doer", session_id))
            r = cur.fetchone()
            c.commit()
        return _session_out(r) if r else None

    def list_sessions(self) -> list[dict]:
        with self._conn() as c, self._cur(c) as cur:
            cur.execute(
                "SELECT s.*, "
                "(SELECT COUNT(*) FROM chat_messages m WHERE m.session_id=s.id) AS n, "
                "(SELECT m.mode FROM chat_messages m WHERE m.session_id=s.id "
                " AND m.role='user' ORDER BY m.id DESC LIMIT 1) AS last_mode "
                "FROM chat_sessions s ORDER BY s.updated_at DESC, s.id DESC")
            rows = cur.fetchall()
        out = []
        for r in rows:
            d = _session_out(r)
            d["message_count"] = r["n"]
            d["last_mode"] = (r.get("last_mode") or "simple")
            out.append(d)
        return out

    def get_session(self, session_id):
        with self._conn() as c, self._cur(c) as cur:
            cur.execute("SELECT * FROM chat_sessions WHERE id=%s", (session_id,))
            r = cur.fetchone()
        return _session_out(r) if r else None

    def get_messages(self, session_id) -> list[dict]:
        with self._conn() as c, self._cur(c) as cur:
            cur.execute(
                "SELECT id, role, content, steps, checkpoint_sha, mode, "
                "duration_s, created_at "
                "FROM chat_messages WHERE session_id=%s ORDER BY id ASC",
                (session_id,))
            rows = cur.fetchall()
        return [_message_out(r) for r in rows]

    def _search_candidates(self, toks, exclude_session) -> list[dict]:
        where = " OR ".join(["strpos(lower(m.content), %s) > 0"] * len(toks))
        params: list = list(toks)
        sql = (
            "SELECT m.session_id AS session_id, m.role AS role, "
            "m.content AS content, m.created_at AS created_at, m.id AS id, "
            "COALESCE(s.title, '') AS session_title "
            "FROM chat_messages m LEFT JOIN chat_sessions s ON s.id = m.session_id "
            f"WHERE ({where}) AND TRIM(m.content) <> ''"
        )
        if exclude_session is not None:
            sql += " AND m.session_id <> %s"
            params.append(exclude_session)
        sql += " ORDER BY m.id DESC LIMIT 500"
        with self._conn() as c, self._cur(c) as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())

    def add_message(self, session_id, role, content, steps=None,
                    mode="simple", duration_s=None) -> int:
        with self._conn() as c, self._cur(c) as cur:
            cur.execute(
                "INSERT INTO chat_messages(session_id, role, content, steps, "
                "mode, duration_s) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                (session_id, role, content, json.dumps(steps or []),
                 mode or "simple", duration_s))
            mid = cur.fetchone()["id"]
            cur.execute("UPDATE chat_sessions SET updated_at=now() WHERE id=%s",
                        (session_id,))
            c.commit()
        return int(mid)

    def set_message_checkpoint(self, message_id, sha) -> None:
        with self._conn() as c, c.cursor() as cur:
            cur.execute("UPDATE chat_messages SET checkpoint_sha=%s WHERE id=%s",
                        (sha or None, message_id))
            c.commit()

    def delete_messages_from(self, session_id, message_id) -> int:
        with self._conn() as c, c.cursor() as cur:
            cur.execute("DELETE FROM chat_messages WHERE session_id=%s AND id>=%s",
                        (session_id, message_id))
            n = cur.rowcount
            cur.execute("UPDATE chat_sessions SET updated_at=now() WHERE id=%s",
                        (session_id,))
            c.commit()
        return n

    def message_checkpoint(self, session_id, message_id):
        with self._conn() as c, self._cur(c) as cur:
            cur.execute(
                "SELECT checkpoint_sha FROM chat_messages WHERE id=%s AND session_id=%s",
                (message_id, session_id))
            r = cur.fetchone()
        return (r["checkpoint_sha"] if r else None) or None

    def rename_session(self, session_id, title):
        with self._conn() as c, self._cur(c) as cur:
            cur.execute("UPDATE chat_sessions SET title=%s, updated_at=now() "
                        "WHERE id=%s RETURNING *",
                        (title.strip() or "New chat", session_id))
            r = cur.fetchone()
            c.commit()
        return _session_out(r) if r else None

    def delete_session(self, session_id) -> bool:
        with self._conn() as c, c.cursor() as cur:
            cur.execute("DELETE FROM chat_media WHERE session_id=%s", (session_id,))
            cur.execute("DELETE FROM chat_messages WHERE session_id=%s", (session_id,))
            cur.execute("DELETE FROM chat_sessions WHERE id=%s", (session_id,))
            deleted = (cur.rowcount or 0) > 0
            c.commit()
        return deleted

    def delete_all_sessions(self) -> int:
        with self._conn() as c, c.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM chat_sessions")
            n = cur.fetchone()[0]
            cur.execute("TRUNCATE chat_media, chat_messages, chat_sessions "
                        "RESTART IDENTITY CASCADE")
            c.commit()
        return int(n or 0)

    def add_media(self, session_id, filename, path, mime="", description="") -> dict:
        with self._conn() as c, self._cur(c) as cur:
            cur.execute(
                "INSERT INTO chat_media(session_id, filename, path, mime, description) "
                "VALUES (%s,%s,%s,%s,%s) RETURNING *",
                (session_id, filename, path, mime or "", description or ""))
            r = cur.fetchone()
            cur2 = c.cursor()
            cur2.execute("UPDATE chat_sessions SET updated_at=now() WHERE id=%s",
                         (session_id,))
            cur2.close()
            c.commit()
        return _media_out(r)

    def list_media(self, session_id) -> list[dict]:
        with self._conn() as c, self._cur(c) as cur:
            cur.execute("SELECT * FROM chat_media WHERE session_id=%s ORDER BY id",
                        (session_id,))
            rows = cur.fetchall()
        return [_media_out(r) for r in rows]

    def get_media(self, media_id):
        with self._conn() as c, self._cur(c) as cur:
            cur.execute("SELECT * FROM chat_media WHERE id=%s", (media_id,))
            r = cur.fetchone()
        return _media_out(r) if r else None

    def set_media_description(self, media_id, description):
        with self._conn() as c, self._cur(c) as cur:
            cur.execute("UPDATE chat_media SET description=%s WHERE id=%s RETURNING *",
                        (description or "", media_id))
            r = cur.fetchone()
            c.commit()
        return _media_out(r) if r else None

    def delete_media(self, media_id):
        with self._conn() as c, self._cur(c) as cur:
            cur.execute("SELECT * FROM chat_media WHERE id=%s", (media_id,))
            r = cur.fetchone()
            if r is None:
                c.rollback()
                return None
            cur.execute("DELETE FROM chat_media WHERE id=%s", (media_id,))
            c.commit()
        return _media_out(r)


# ═══════════════════════════ backend selection ═══════════════════════════════

_BACKEND = None
_BACKEND_LOCK = threading.Lock()


def _backend():
    """Pick the chat backend once per process — Postgres when AIFORGE_PG_URL is
    set (the data-driven stack), else embedded SQLite (the --lite default)."""
    global _BACKEND
    if _BACKEND is not None:
        return _BACKEND
    with _BACKEND_LOCK:
        if _BACKEND is None:
            if getattr(_env, "AIFORGE_USE_SQLITE", True):
                _BACKEND = _SqliteChatStore()
            else:
                # Postgres configured but maybe unreachable (no Docker / PG down)
                # → degrade to embedded SQLite instead of failing every chat
                # turn. AIFORGE_REQUIRE_PG=1 hard-fails instead.
                try:
                    be = _PgChatStore(_env.AIFORGE_PG_URL)
                    with be._conn():                   # probe: real connection
                        pass
                    _BACKEND = be
                except Exception as exc:  # noqa: BLE001
                    import os
                    if os.environ.get("AIFORGE_REQUIRE_PG") == "1":
                        raise
                    import logging
                    logging.getLogger("aiforge.chat").warning(
                        "Postgres unreachable (%s) — chat using embedded SQLite", exc)
                    _BACKEND = _SqliteChatStore()
    return _BACKEND


def reset_backend_for_tests():
    """Test hook — drop the memoized backend so env changes take effect."""
    global _BACKEND
    _BACKEND = None


# ═══════════════════════════ public function API ═════════════════════════════
# Every signature + return shape is preserved from the pre-refactor SQLite
# module; callers are unchanged.

def create_session(title: str = "New chat", cwd: str | None = None,
                   role: str = "chat") -> dict:
    return _backend().create_session(title, cwd, role)


def set_session_cwd(session_id: int, cwd: str) -> "dict | None":
    return _backend().set_session_cwd(session_id, cwd)


def set_session_role(session_id: int, role: str) -> "dict | None":
    return _backend().set_session_role(session_id, role)


def list_sessions() -> list[dict]:
    return _backend().list_sessions()


def get_session(session_id: int) -> "dict | None":
    return _backend().get_session(session_id)


def get_messages(session_id: int) -> list[dict]:
    return _backend().get_messages(session_id)


def search_messages(query: str, *, limit: int = 6,
                    exclude_session: "int | None" = None) -> list[dict]:
    """Full-text-ish search over prior chat message CONTENT (all sessions except
    ``exclude_session``). Cheap + local: one indexed-ish scan, no LLM, no
    network. A message matches if its content contains ANY query token (tokens:
    lowercase alphanumeric, len>=3, common stopwords dropped). Ranked by
    (# distinct tokens matched desc, then recency desc). Returns up to ``limit``
    hits: ``[{"session_id","session_title","role","content","created_at"}]``
    with each content truncated to ~300 chars. Soft-fail: any error → []."""
    toks = _tokens(query)
    if not toks:
        return []
    try:
        rows = _backend()._search_candidates(toks, exclude_session)
    except Exception:  # noqa: BLE001 — search must never break a chat turn
        return []
    return _rank_search(rows, toks, limit)


def add_message(session_id: int, role: str, content: str,
                steps: "list | None" = None, mode: str = "simple",
                duration_s: "float | None" = None) -> int:
    return _backend().add_message(session_id, role, content, steps, mode,
                                  duration_s)


def set_message_checkpoint(message_id: int, sha: str) -> None:
    """Stamp the workspace checkpoint sha taken just before this message — so
    edit-resend can restore the tree to exactly that turn's state."""
    return _backend().set_message_checkpoint(message_id, sha)


def delete_messages_from(session_id: int, message_id: int) -> int:
    """Delete this message and every message after it in the session (by the
    stable autoincrement id ordering). Returns the number of rows removed.
    Used by edit-and-resend: truncate history at the edited turn before re-running."""
    return _backend().delete_messages_from(session_id, message_id)


def message_checkpoint(session_id: int, message_id: int) -> "str | None":
    """The checkpoint sha stamped on a given message, or None."""
    return _backend().message_checkpoint(session_id, message_id)


def rename_session(session_id: int, title: str) -> "dict | None":
    return _backend().rename_session(session_id, title)


def delete_session(session_id: int) -> bool:
    return _backend().delete_session(session_id)


def delete_all_sessions() -> int:
    """Delete EVERY chat session + its messages and reset the id sequence so new
    sessions start at 1. Returns the count of sessions deleted."""
    return _backend().delete_all_sessions()


# ── Chat media (uploaded images + their descriptions) ─────────────────────────

def add_media(session_id: int, filename: str, path: str,
              mime: str = "", description: str = "") -> dict:
    return _backend().add_media(session_id, filename, path, mime, description)


def list_media(session_id: int) -> list[dict]:
    return _backend().list_media(session_id)


def get_media(media_id: int) -> "dict | None":
    return _backend().get_media(media_id)


def set_media_description(media_id: int, description: str) -> "dict | None":
    return _backend().set_media_description(media_id, description)


def delete_media(media_id: int) -> "dict | None":
    """Delete the row, returning it (so the caller can unlink the file)."""
    return _backend().delete_media(media_id)
