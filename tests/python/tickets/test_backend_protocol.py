"""The storage-backend protocol the ticket store is written against.

``StoreBackend`` is a runtime-checkable Protocol that documents every raw op a
backend must provide — and nothing imported it, so it was 0% covered and a
backend could quietly stop satisfying it. This pins the shape: the shipped
SQLite backend implements the protocol, and a partial one does not.
"""
from __future__ import annotations

import inspect

from aiforge_core.tickets.backends.base import StoreBackend


def test_a_backend_missing_an_operation_does_not_satisfy_it():
    """`isinstance`, not `issubclass`: StoreBackend carries a `name`
    attribute, and a Protocol with a non-method member only supports the
    instance check."""
    class Partial:
        name = "partial"

        def ensure_schema(self) -> None:
            pass

    assert not isinstance(Partial(), StoreBackend)


def test_every_documented_operation_is_named_in_the_protocol():
    """The protocol IS the contract store.py writes against; a method added to
    a backend without being declared here is undocumented surface."""
    declared = {n for n, _ in inspect.getmembers(StoreBackend)
                if not n.startswith("_")}
    for op in ("ensure_schema", "next_counter", "insert_ticket",
               "fetch_ticket", "claim_oldest", "reap_stale_in_progress",
               "set_status", "delete_ticket", "reset_all_tickets",
               "set_route", "set_branch", "append_body", "insert_event",
               "fetch_events", "list_tickets", "get_enriched",
               "fetch_children", "search_title"):
        assert op in declared, f"{op} is not declared on StoreBackend"


def test_the_sqlite_backend_declares_every_protocol_operation():
    from aiforge_core.tickets.backends.sqlite_backend import SqliteBackend

    for name, _ in inspect.getmembers(StoreBackend, inspect.isfunction):
        if name.startswith("_"):
            continue
        assert callable(getattr(SqliteBackend, name, None)), \
            f"SqliteBackend is missing {name}"
