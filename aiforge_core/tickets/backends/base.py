"""Storage backend protocol for the ticket store.

Both PgBackend and SqliteBackend implement this surface. store.py
delegates its module-level functions to whichever the factory picks.
All methods return plain dict rows (psycopg dict_row shape); store.py
wraps them into Ticket via Ticket.from_row.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class StoreBackend(Protocol):
    def ensure_schema(self) -> None: ...

    def new_identifier(self) -> str: ...

    def create(self, fields: dict) -> dict: ...

    def get(self, ident_or_id: "str | int") -> "dict | None": ...

    def claim_next_any(self, aliases: list[str], excluded_projects: list[str]) -> "dict | None": ...

    def update_status(self, ticket_id: int, status: str, role: "str | None",
                      extra: dict) -> "dict | None": ...

    def update_route(self, ticket_id: int, route: str, workflow: "str | None",
                     source: str, confidence: "float | None") -> "dict | None": ...

    def add_comment(self, ticket_id: int, role: "str | None", body: str) -> int: ...

    def add_event(self, ticket_id: int, role: "str | None", kind: str,
                  body: "str | None", metadata: "dict | None") -> int: ...

    def children(self, parent_id: int) -> list[dict]: ...

    def by_title_project(self, title: str, project: "str | None") -> list[dict]: ...

    def comments(self, ticket_id: int, limit: int) -> list[dict]: ...
