"""Postgres implementation of StoreBackend — the original store SQL.

Raw dialect-specific ops only; all business logic lives in store.py.
Used only when AIFORGE_PG_URL is set. The SQL here is the historical
store.py SQL, sliced into the raw-ops contract. Returns dict rows;
store.py wraps them in Ticket.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

# DDL copied verbatim from store.py's _SCHEMA_SQL — kept here so this
# module is self-contained (no circular import with store.py).
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
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS claimed_at        timestamptz;

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

_PRIORITY_ORDER = (
    "CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 "
    "WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END"
)

_INSERT_COLS = (
    "identifier", "title", "body", "priority", "assignee_role", "parent_id",
    "project", "labels", "branch", "metadata", "route", "route_workflow",
    "route_source", "route_confidence",
)

# Correlated subqueries shared by the enriched list/detail queries.
_STARTED_AT = (
    "(SELECT MIN(created_at) FROM ticket_events "
    " WHERE ticket_id = tickets.id AND kind='status_change' AND body='in_progress')"
    " AS started_at"
)
_ACTIVE_ROLE = (
    "(SELECT agent_role FROM ticket_events "
    " WHERE ticket_id = tickets.id AND agent_role IS NOT NULL "
    " ORDER BY created_at DESC LIMIT 1) AS active_role"
)


class PgBackend:
    name = "postgres"

    def __init__(self, dsn: str):
        self.dsn = dsn

    @contextmanager
    def _conn(self) -> Iterator[psycopg.Connection]:
        c = psycopg.connect(self.dsn, autocommit=False, connect_timeout=5,
                            options="-c statement_timeout=15000")
        try:
            yield c
        finally:
            c.close()

    def ensure_schema(self) -> None:
        with self._conn() as c:
            try:
                with c.cursor() as cur:
                    cur.execute(_PG_DDL)
                c.commit()
            except Exception:
                c.rollback()

    def next_counter(self) -> int:
        with self._conn() as c, c.cursor() as cur:
            cur.execute(
                "UPDATE ticket_counter SET next_n = next_n + 1 WHERE singleton "
                "RETURNING next_n - 1"
            )
            n = cur.fetchone()[0]
            c.commit()
        return int(n)

    def insert_ticket(self, fields: dict) -> dict:
        with self._conn() as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                INSERT INTO tickets
                  (identifier, title, body, priority, assignee_role,
                   parent_id, project, labels, branch, metadata,
                   route, route_workflow, route_source, route_confidence)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s)
                RETURNING *;
                """,
                (
                    fields["identifier"], fields["title"], fields.get("body", ""),
                    fields.get("priority", "medium"), fields.get("assignee_role"),
                    fields.get("parent_id"), fields.get("project"),
                    fields.get("labels") or [], fields.get("branch"),
                    json.dumps(fields.get("metadata") or {}),
                    fields.get("route", "code"), fields.get("route_workflow"),
                    fields.get("route_source", "auto"), fields.get("route_confidence"),
                ),
            )
            row = cur.fetchone()
            c.commit()
        return row

    def fetch_ticket(self, ident_or_id) -> "dict | None":
        with self._conn() as c, c.cursor(row_factory=dict_row) as cur:
            if isinstance(ident_or_id, int):
                cur.execute("SELECT * FROM tickets WHERE id=%s", (ident_or_id,))
            else:
                cur.execute("SELECT * FROM tickets WHERE identifier=%s", (ident_or_id,))
            row = cur.fetchone()
        return row

    def claim_oldest(self, excluded_projects) -> "dict | None":
        with self._conn() as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM tickets "
                "WHERE status='todo' "
                "  AND (project IS NULL OR project <> ALL(%s)) "
                f"ORDER BY {_PRIORITY_ORDER}, created_at ASC, id ASC LIMIT 1 "
                "FOR UPDATE SKIP LOCKED",
                (excluded_projects,),
            )
            row = cur.fetchone()
            if row is None:
                c.rollback()
                return None
            cur.execute(
                "UPDATE tickets SET status='in_progress', claimed_at=now() "
                "WHERE id=%s RETURNING *",
                (row["id"],),
            )
            row = cur.fetchone()
            c.commit()
        return row

    def reap_stale_in_progress(self, max_age_s) -> list[int]:
        """Reset ``in_progress`` rows whose claim is older than the lease back
        to ``todo`` (a hard-crashed / OOM-killed / redeployed runner never
        clears its own claim, and re-claim only selects ``todo``). Bumps
        ``metadata.reclaim_count`` so the dashboard metric becomes real.
        Falls back to ``updated_at`` for pre-migration rows with a NULL
        claimed_at. Returns the list of reset ticket ids."""
        with self._conn() as c, c.cursor() as cur:
            cur.execute(
                "UPDATE tickets SET status='todo', claimed_at=NULL, "
                "  metadata = jsonb_set(metadata, '{reclaim_count}', "
                "    to_jsonb(COALESCE((metadata->>'reclaim_count')::int, 0) + 1)) "
                "WHERE status='in_progress' "
                "  AND COALESCE(claimed_at, updated_at) "
                "      < now() - make_interval(secs => %s) "
                "RETURNING id",
                (int(max_age_s),),
            )
            ids = [int(r[0]) for r in cur.fetchall()]
            c.commit()
        return ids

    def set_status(self, ticket_id, status, completed, metadata_patch) -> "dict | None":
        completed_at = "now()" if completed else "completed_at"
        with self._conn() as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"UPDATE tickets SET status=%s, completed_at={completed_at}, "
                "metadata = metadata || %s::jsonb WHERE id=%s RETURNING *;",
                (status, json.dumps(metadata_patch or {}), ticket_id),
            )
            row = cur.fetchone()
            c.commit()
        return row

    def delete_ticket(self, ticket_id) -> bool:
        with self._conn() as c, c.cursor() as cur:
            cur.execute("DELETE FROM ticket_events WHERE ticket_id=%s", (ticket_id,))
            cur.execute("DELETE FROM tickets WHERE id=%s", (ticket_id,))
            deleted = (cur.rowcount or 0) > 0
            c.commit()
        return deleted

    def reset_all_tickets(self) -> int:
        """Delete ALL tickets + events and reset the ONE-<n> counter to its
        seed (100) and the row identity, so the sequence starts over."""
        with self._conn() as c, c.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM tickets")
            n = cur.fetchone()[0]
            cur.execute("TRUNCATE ticket_events, tickets RESTART IDENTITY CASCADE")
            cur.execute("UPDATE ticket_counter SET next_n = 100 WHERE singleton")
            c.commit()
        return int(n or 0)

    def set_route(self, ident_or_id, route, workflow, source, confidence) -> "dict | None":
        where = "id=%s" if isinstance(ident_or_id, int) else "identifier=%s"
        with self._conn() as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"UPDATE tickets SET route=%s, route_workflow=%s, "
                f"  route_source=%s, route_confidence=%s WHERE {where} RETURNING *",
                (route, workflow, source, confidence, ident_or_id),
            )
            row = cur.fetchone()
            c.commit()
        return row

    def set_branch(self, ticket_id, branch) -> None:
        with self._conn() as c, c.cursor() as cur:
            cur.execute("UPDATE tickets SET branch=%s WHERE id=%s",
                        (branch, ticket_id))
            c.commit()

    def append_body(self, ticket_id, extra) -> "dict | None":
        with self._conn() as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "UPDATE tickets SET body = body || %s WHERE id=%s RETURNING *",
                (extra, ticket_id))
            row = cur.fetchone()
            c.commit()
        return row

    def insert_event(self, ticket_id, agent_role, kind, body, metadata) -> int:
        with self._conn() as c, c.cursor() as cur:
            cur.execute(
                "INSERT INTO ticket_events (ticket_id, agent_role, kind, body, metadata) "
                "VALUES (%s,%s,%s,%s,%s::jsonb) RETURNING id",
                (ticket_id, agent_role, kind, body, json.dumps(metadata or {})),
            )
            eid = cur.fetchone()[0]
            c.commit()
        return int(eid)

    def fetch_events(self, ticket_id, limit) -> list[dict]:
        with self._conn() as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, ticket_id, created_at, agent_role, kind, body, metadata "
                "FROM ticket_events WHERE ticket_id=%s "
                "ORDER BY created_at ASC, id ASC LIMIT %s",
                (ticket_id, limit),
            )
            return list(cur.fetchall())

    def list_tickets(self, role, statuses, parent_identifier, limit) -> list[dict]:
        clauses: list[str] = []
        params: list = []
        if role:
            clauses.append("assignee_role = %s")
            params.append(role)
        if statuses:
            clauses.append("status = ANY(%s)")
            params.append(list(statuses))
        if parent_identifier:
            clauses.append(
                "parent_id = (SELECT id FROM tickets WHERE identifier=%s)")
            params.append(parent_identifier)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        q = (
            f"SELECT tickets.*, {_STARTED_AT}, {_ACTIVE_ROLE} "
            f"FROM tickets{where} ORDER BY id DESC LIMIT %s"
        )
        params.append(limit)
        with self._conn() as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(q, params)
            return list(cur.fetchall())

    def get_enriched(self, identifier) -> "dict | None":
        with self._conn() as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT tickets.*, {_STARTED_AT}, {_ACTIVE_ROLE} "
                "FROM tickets WHERE identifier=%s",
                (identifier,),
            )
            return cur.fetchone()

    def fetch_children(self, parent_id) -> list[dict]:
        with self._conn() as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM tickets WHERE parent_id=%s ORDER BY created_at ASC, id ASC",
                (parent_id,),
            )
            return list(cur.fetchall())

    def search_title(self, needle, project, statuses) -> list[dict]:
        with self._conn() as c, c.cursor(row_factory=dict_row) as cur:
            if project:
                cur.execute(
                    "SELECT * FROM tickets "
                    "WHERE lower(title)=%s AND project=%s "
                    "AND status = ANY(%s) "
                    "ORDER BY created_at ASC, id ASC LIMIT 20",
                    (needle, project, statuses),
                )
            else:
                cur.execute(
                    "SELECT * FROM tickets WHERE lower(title)=%s "
                    "AND status = ANY(%s) "
                    "ORDER BY created_at ASC, id ASC LIMIT 20",
                    (needle, statuses),
                )
            return list(cur.fetchall())
