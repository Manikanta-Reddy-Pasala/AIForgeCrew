"""Postgres backend for the chat store (the data-driven stack)."""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager

from ._helpers import _media_out, _message_out, _session_out

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
        if not toks:                       # empty tokens → invalid SQL (WHERE ())
            return []
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
