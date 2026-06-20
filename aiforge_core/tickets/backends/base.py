"""Raw storage-backend protocol for the ticket store.

Design: ALL business logic (supervisor invariants, status-change event
writes, priority ordering policy, Ticket wrapping, validation, the
ONE-<n> counter start) lives in ``store.py``. A backend only does the
dialect-specific raw SQL ops below, returning plain dict rows
(psycopg ``dict_row`` shape; SQLite parses JSON columns to match).

This split keeps the Postgres path byte-identical to the historical
behavior while letting the embedded SQLite backend share the exact same
higher-level semantics. Both PgBackend and SqliteBackend implement this.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class StoreBackend(Protocol):
    name: str

    def ensure_schema(self) -> None:
        """Idempotently create tables/indexes/counter. Best-effort."""
        ...

    def next_counter(self) -> int:
        """Atomically allocate and return the next ticket number.

        Mirrors the historical Postgres counter: seeded at 100, the
        first allocation returns 100. store.py formats ``ONE-{n}``.
        """
        ...

    def insert_ticket(self, fields: dict) -> dict:
        """Insert one ticket row from a fields dict and return it.

        Recognized keys (status defaults to 'todo' at the DB level):
        identifier, title, body, priority, assignee_role, parent_id,
        project, labels (list), branch, metadata (dict), route,
        route_workflow, route_source, route_confidence.
        """
        ...

    def fetch_ticket(self, ident_or_id: "str | int") -> "dict | None":
        """int -> by id; str -> by identifier."""
        ...

    def claim_oldest(self, excluded_projects: list[str]) -> "dict | None":
        """Atomically claim the oldest todo ticket across ALL roles,
        ordered by priority (urgent>high>medium>low) then created_at,
        excluding the given projects (NULL project always claimable),
        mark it in_progress, and return the row. Does NOT write the
        status-change event — store.py does that. Returns None if none.
        """
        ...

    def set_status(self, ticket_id: int, status: str, completed: bool,
                   metadata_patch: dict) -> "dict | None":
        """Update status, set completed_at=now() when ``completed``,
        shallow-merge metadata_patch into metadata. Return the row."""
        ...

    def set_route(self, ident_or_id: "str | int", route: str,
                  workflow: "str | None", source: str,
                  confidence: "float | None") -> "dict | None":
        ...

    def set_branch(self, ticket_id: int, branch: str) -> None:
        """Set a ticket's git branch."""
        ...

    def insert_event(self, ticket_id: int, agent_role: "str | None", kind: str,
                     body: "str | None", metadata: dict) -> int:
        """Insert a ticket_event, return its id."""
        ...

    def fetch_events(self, ticket_id: int, limit: int) -> list[dict]:
        """Return full event rows for a ticket (ALL kinds), oldest-first,
        each with id, ticket_id, agent_role, kind, body, metadata,
        created_at. Used by store.comments() and the API ticket detail."""
        ...

    def list_tickets(self, role: "str | None", statuses: "list[str] | None",
                     parent_identifier: "str | None", limit: int) -> list[dict]:
        """List tickets with optional role/status/parent filters, newest
        first. Rows are enriched with ``started_at`` (first in_progress
        event time) and ``active_role`` (most recent agent_role on the
        ticket) for the dashboard. Used by the API."""
        ...

    def get_enriched(self, identifier: str) -> "dict | None":
        """Fetch one ticket by identifier, enriched with ``started_at``
        and ``active_role`` (see list_tickets)."""
        ...

    def fetch_children(self, parent_id: int) -> list[dict]:
        ...

    def search_title(self, needle: str, project: "str | None",
                     statuses: list[str]) -> list[dict]:
        """Find tickets where lower(title)==needle, optional project
        match, status in ``statuses``, oldest-first, limit 20."""
        ...
