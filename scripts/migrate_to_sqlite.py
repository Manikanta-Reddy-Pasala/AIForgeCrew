"""Move data: Postgres (chat + tickets) → SQLite, for the zero-docker (--lite)
switch. Reads via the PG backends, writes via the SQLite backends — both
instantiated directly so one process spans both. Run with AIFORGE_PG_URL set to
the source Postgres; the SQLite destinations are the app's default --lite stores.

    AIFORGE_PG_URL=postgresql://aiforge:aiforgepass@127.0.0.1:5432/aiforge \\
        ./.venv/bin/python scripts/migrate_to_sqlite.py

Idempotent-ish: tickets skip a duplicate identifier; chat appends (run once).
Stop the aiforge-api service first so the SQLite files aren't write-locked.
"""
from __future__ import annotations

import os
import sys


def migrate_chat(pg_url: str) -> tuple[int, int]:
    from aiforge_core.runtime.chat_store import _PgChatStore, _SqliteChatStore
    src = _PgChatStore(pg_url)
    dst = _SqliteChatStore()
    # IDEMPOTENT: chat has no natural unique key and create_session always makes a
    # NEW row, so re-running would DUPLICATE every session on each boot. If the
    # SQLite chat store already has sessions, it was already migrated → skip.
    try:
        if dst.list_sessions():
            print("  chat: SQLite already has sessions — skip (already migrated)")
            return 0, 0
    except Exception:  # noqa: BLE001 — best-effort; proceed if it can't check
        pass
    n_s = n_m = 0
    for s in src.list_sessions():
        new = dst.create_session(title=s.get("title") or "chat",
                                 cwd=s.get("cwd"), role=s.get("role") or "chat")
        n_s += 1
        for m in src.get_messages(s["id"]):
            try:
                dst.add_message(new["id"], m.get("role") or "user",
                                m.get("content") or "", m.get("steps"))
                n_m += 1
            except Exception as exc:  # noqa: BLE001
                print(f"  chat msg skip (session {s['id']}): {exc}", file=sys.stderr)
    return n_s, n_m


_TICKET_FIELDS = ("identifier", "title", "body", "priority", "assignee_role",
                  "project", "labels", "branch", "metadata", "route",
                  "route_workflow", "route_source", "route_confidence")


def migrate_tickets(pg_url: str) -> int:
    from aiforge_core.tickets.backend_factory import get_backend
    from aiforge_core.tickets.backends.pg_backend import PgBackend
    src = PgBackend(pg_url)
    dst = get_backend()          # SqliteBackend in --lite (no AIFORGE_PG_URL)
    if hasattr(dst, "ensure_schema"):
        dst.ensure_schema()
    rows = sorted(src.list_tickets(None, None, None, 1_000_000),
                  key=lambda r: r["id"])          # parents (lower id) first
    old2new: dict[int, int] = {}
    n = 0
    for row in rows:
        full = src.fetch_ticket(row["id"]) or row
        fields = {k: full.get(k) for k in _TICKET_FIELDS}
        # remap parent_id via the already-migrated map (drop if unresolved)
        pid = full.get("parent_id")
        if pid and pid in old2new:
            fields["parent_id"] = old2new[pid]
        try:
            new = dst.insert_ticket(fields)
        except Exception as exc:  # noqa: BLE001 — dup identifier / bad row
            print(f"  ticket skip {full.get('identifier')}: {exc}", file=sys.stderr)
            continue
        old2new[row["id"]] = new["id"]
        n += 1
        st = full.get("status")
        if st and st != "todo" and hasattr(dst, "set_status"):
            try:
                dst.set_status(new["id"], st, full.get("completed"), None)
            except Exception:  # noqa: BLE001
                pass
        for e in src.fetch_events(row["id"], 1_000_000):
            try:
                dst.insert_event(new["id"], e.get("agent_role"), e.get("kind"),
                                 e.get("body"), e.get("metadata"))
            except Exception:  # noqa: BLE001
                pass
    _advance_ticket_counter(dst)
    return n


def _advance_ticket_counter(dst) -> None:
    """After inserting tickets with their ORIGINAL identifiers, bump the SQLite
    ticket_counter past the max — else new_identifier() reuses a taken id and
    the next create hits UNIQUE constraint failed: tickets.identifier."""
    import re
    import sqlite3
    path = getattr(dst, "path", None) or getattr(dst, "_path", None)
    if not path:
        return
    try:
        with sqlite3.connect(path) as c:
            nums = [int(m.group(1))
                    for (i,) in c.execute("SELECT identifier FROM tickets")
                    if (m := re.search(r"-(\d+)$", i or ""))]
            if nums:
                c.execute("UPDATE ticket_counter SET next_n=? "
                          "WHERE singleton=1 AND next_n <= ?",
                          (max(nums) + 1, max(nums)))
                c.commit()
    except Exception as exc:  # noqa: BLE001
        import sys
        print(f"  counter advance skipped: {exc}", file=sys.stderr)


def main() -> int:
    pg = os.environ.get("AIFORGE_PG_URL")
    if not pg:
        print("set AIFORGE_PG_URL to the source Postgres", file=sys.stderr)
        return 2
    # Read from PG explicitly; the SQLite dest must NOT resolve to PG, so clear
    # the env the sqlite backend would otherwise pick up.
    os.environ.pop("AIFORGE_PG_URL", None)
    os.environ.pop("AIFORGE_DSN", None)
    os.environ.pop("AIFORGE_FORCE_PG", None)
    print("→ chat …")
    cs, cm = migrate_chat(pg)
    print(f"  migrated {cs} sessions, {cm} messages")
    print("→ tickets …")
    nt = migrate_tickets(pg)
    print(f"  migrated {nt} tickets (+ events)")
    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
