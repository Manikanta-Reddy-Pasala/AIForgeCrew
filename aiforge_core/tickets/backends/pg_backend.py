"""Postgres implementation of StoreBackend — the original store logic.

Ported verbatim from store.py. Used only when AIFORGE_PG_URL is set.
Returns raw dict rows; store.py wraps them in Ticket.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

# DDL copied verbatim from store.py's _SCHEMA_SQL — kept here so this
# module has zero import dependency on store.py (avoids circular imports).
_PG_DDL = """
CREATE TABLE IF NOT EXISTS tickets (
  id            bigserial PRIMARY KEY,
  identifier    text UNIQUE NOT NULL,
  title         text NOT NULL,
  body          text NOT NULL DEFAULT '',
  status        text NOT NULL DEFAULT 'todo',
  priority      text NOT NULL DEFAULT 'medium',
  assignee_role text,
  parent_id     bigint REFERENCES tickets(id) ON DELETE CASCADE,
  branch        text,
  project       text,
  labels        text[] NOT NULL DEFAULT '{}',
  metadata      jsonb  NOT NULL DEFAULT '{}'::jsonb,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  completed_at  timestamptz
);

ALTER TABLE tickets ADD COLUMN IF NOT EXISTS route             text NOT NULL DEFAULT 'code';
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS route_workflow    text;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS route_source      text NOT NULL DEFAULT 'auto';
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS route_confidence  real;

CREATE INDEX IF NOT EXISTS tickets_assignee_status ON tickets(assignee_role, status);
CREATE INDEX IF NOT EXISTS tickets_parent ON tickets(parent_id);
CREATE INDEX IF NOT EXISTS tickets_status ON tickets(status);
CREATE INDEX IF NOT EXISTS tickets_route ON tickets(route, route_workflow);

CREATE TABLE IF NOT EXISTS ticket_events (
  id         bigserial PRIMARY KEY,
  ticket_id  bigint NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
  agent_role text,
  kind       text NOT NULL,
  body       text,
  metadata   jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ticket_events_ticket_ts ON ticket_events(ticket_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ticket_events_kind ON ticket_events(kind);

CREATE TABLE IF NOT EXISTS ticket_counter (
  singleton boolean PRIMARY KEY DEFAULT TRUE,
  next_n    bigint  NOT NULL
);
INSERT INTO ticket_counter (singleton, next_n) VALUES (TRUE, 100)
  ON CONFLICT DO NOTHING;

CREATE OR REPLACE FUNCTION tickets_touch_updated_at() RETURNS trigger
  LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at := now(); RETURN NEW; END $$;

DROP TRIGGER IF EXISTS tickets_updated_at ON tickets;
CREATE TRIGGER tickets_updated_at BEFORE UPDATE ON tickets
  FOR EACH ROW EXECUTE FUNCTION tickets_touch_updated_at();
"""


class PgBackend:
    name = "postgres"

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._schema_bootstrapped = False

    @contextmanager
    def _conn(self) -> Iterator[psycopg.Connection]:
        c = psycopg.connect(self.dsn, autocommit=False, connect_timeout=5,
                            options="-c statement_timeout=15000")
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def ensure_schema(self) -> None:
        """Run the DDL bootstrap. Idempotent (all statements use IF NOT EXISTS)."""
        if self._schema_bootstrapped:
            return
        try:
            with psycopg.connect(self.dsn, autocommit=False, connect_timeout=5,
                                 options="-c statement_timeout=15000") as c:
                with c.cursor() as cur:
                    cur.execute(_PG_DDL)
                c.commit()
        except Exception:
            pass
        self._schema_bootstrapped = True

    def new_identifier(self) -> str:
        """Atomic ONE-<n> allocator using the singleton ticket_counter row."""
        with self._conn() as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE ticket_counter SET next_n = next_n + 1 WHERE singleton "
                    "RETURNING next_n - 1"
                )
                n = cur.fetchone()[0]
        return f"ONE-{n}"

    def create(self, fields: dict) -> dict:
        """Insert a ticket row and return the raw dict row.

        ``fields`` must contain at least ``identifier`` and ``title``.
        ``new_identifier()`` is called by store.py before create() so
        ``fields['identifier']`` is always present here.
        """
        with self._conn() as c:
            with c.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    INSERT INTO tickets
                      (identifier, title, body, status, priority, assignee_role,
                       parent_id, branch, project, labels, metadata,
                       route, route_workflow, route_source, route_confidence)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s)
                    RETURNING *
                    """,
                    (
                        fields["identifier"],
                        fields["title"],
                        fields.get("body", ""),
                        fields.get("status", "todo"),
                        fields.get("priority", "medium"),
                        fields.get("assignee_role"),
                        fields.get("parent_id"),
                        fields.get("branch"),
                        fields.get("project"),
                        list(fields.get("labels") or []),
                        json.dumps(fields.get("metadata") or {}),
                        fields.get("route", "code"),
                        fields.get("route_workflow"),
                        fields.get("route_source", "auto"),
                        fields.get("route_confidence"),
                    ),
                )
                return cur.fetchone()

    def get(self, ident_or_id) -> "dict | None":
        with self._conn() as c:
            with c.cursor(row_factory=dict_row) as cur:
                if isinstance(ident_or_id, int):
                    cur.execute("SELECT * FROM tickets WHERE id=%s", (ident_or_id,))
                else:
                    cur.execute("SELECT * FROM tickets WHERE identifier=%s",
                                (str(ident_or_id),))
                return cur.fetchone()

    def claim_next_any(self, aliases: list[str],
                       excluded_projects: list[str]) -> "dict | None":
        """Atomically claim the oldest eligible todo ticket.

        ``aliases`` and ``excluded_projects`` are pre-computed by store.py
        (via _aliases_for / _excluded_projects) and passed in directly.
        Uses SELECT … FOR UPDATE SKIP LOCKED so concurrent runners cannot
        double-claim.
        """
        if not aliases:
            return None
        with self._conn() as c:
            with c.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT * FROM tickets "
                    "WHERE status='todo' "
                    "  AND assignee_role = ANY(%s) "
                    "  AND (project IS NULL OR project <> ALL(%s)) "
                    "ORDER BY CASE priority "
                    "  WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 "
                    "  WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END, "
                    "created_at ASC LIMIT 1 "
                    "FOR UPDATE SKIP LOCKED",
                    (aliases, excluded_projects),
                )
                row = cur.fetchone()
                if row is None:
                    c.rollback()
                    return None
                cur.execute(
                    "UPDATE tickets SET status='in_progress' WHERE id=%s RETURNING *",
                    (row["id"],),
                )
                row = cur.fetchone()
                cur.execute(
                    "INSERT INTO ticket_events (ticket_id, agent_role, kind, body) "
                    "VALUES (%s, %s, 'status_change', 'in_progress')",
                    (row["id"], "graph_runner"),
                )
        return row

    def update_status(self, ticket_id: int, status: str,
                      role: "str | None", extra: dict) -> "dict | None":
        """Update ticket status and optional extra fields (branch, assignee_role, parent_id).

        ``extra`` may contain ``branch``, ``assignee_role``, ``parent_id``.
        Completed-at is set for done/cancelled/qa_failed (mirrors store.py).
        Status validation is NOT performed here — store.py handles that.
        """
        completed_at_expr = (
            "now()" if status in ("done", "cancelled", "qa_failed") else "completed_at"
        )
        # Build dynamic SET clause for optional extra fields
        extra_sets = []
        extra_params = []
        for k in ("branch", "assignee_role", "parent_id"):
            if k in extra:
                extra_sets.append(f"{k}=%s")
                extra_params.append(extra[k])

        extra_clause = (", " + ", ".join(extra_sets)) if extra_sets else ""

        with self._conn() as c:
            with c.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    f"UPDATE tickets SET status=%s, completed_at={completed_at_expr}"
                    f"{extra_clause} WHERE id=%s RETURNING *",
                    [status] + extra_params + [ticket_id],
                )
                row = cur.fetchone()
                cur.execute(
                    "INSERT INTO ticket_events (ticket_id, agent_role, kind, body) "
                    "VALUES (%s, %s, 'status_change', %s)",
                    (ticket_id, role, status),
                )
        return row

    def update_route(self, ticket_id: int, route: str, workflow: "str | None",
                     source: str, confidence: "float | None") -> "dict | None":
        with self._conn() as c:
            with c.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "UPDATE tickets SET route=%s, route_workflow=%s, "
                    "  route_source=%s, route_confidence=%s "
                    "WHERE id=%s RETURNING *",
                    (route, workflow, source, confidence, ticket_id),
                )
                return cur.fetchone()

    def add_event(self, ticket_id: int, role: "str | None", kind: str,
                  body: "str | None", metadata: "dict | None") -> int:
        with self._conn() as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO ticket_events (ticket_id, agent_role, kind, body, metadata) "
                    "VALUES (%s,%s,%s,%s,%s::jsonb) RETURNING id",
                    (ticket_id, role, kind, body, json.dumps(metadata or {})),
                )
                return cur.fetchone()[0]

    def add_comment(self, ticket_id: int, role: "str | None", body: str) -> int:
        return self.add_event(ticket_id, role, "comment", body, {})

    def comments(self, ticket_id: int, limit: int) -> list[dict]:
        with self._conn() as c:
            with c.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT created_at, agent_role, kind, body, metadata "
                    "FROM ticket_events WHERE ticket_id=%s "
                    "ORDER BY created_at ASC LIMIT %s",
                    (ticket_id, limit),
                )
                return list(cur.fetchall())

    def children(self, parent_id: int) -> list[dict]:
        with self._conn() as c:
            with c.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT * FROM tickets WHERE parent_id=%s ORDER BY created_at ASC",
                    (parent_id,),
                )
                return list(cur.fetchall())

    def by_title_project(self, title: str, project: "str | None") -> list[dict]:
        if not title:
            return []
        needle = title.strip().lower()
        with self._conn() as c:
            with c.cursor(row_factory=dict_row) as cur:
                if project:
                    cur.execute(
                        "SELECT * FROM tickets "
                        "WHERE lower(title)=%s AND project=%s "
                        "AND status IN ('todo','in_progress','in_review','qa','qa_failed','blocked','done') "
                        "ORDER BY created_at ASC LIMIT 20",
                        (needle, project),
                    )
                else:
                    cur.execute(
                        "SELECT * FROM tickets WHERE lower(title)=%s "
                        "AND status IN ('todo','in_progress','in_review','qa','qa_failed','blocked','done') "
                        "ORDER BY created_at ASC LIMIT 20",
                        (needle,),
                    )
                return list(cur.fetchall())
