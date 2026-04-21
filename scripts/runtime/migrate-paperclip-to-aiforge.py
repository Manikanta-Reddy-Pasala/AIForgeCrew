#!/usr/bin/env python3
"""Export open/recent Paperclip issues into aiforge.tickets.

Run ON Mac Studio (both Postgres clusters are local there):

    python scripts/runtime/migrate-paperclip-to-aiforge.py --all
    python scripts/runtime/migrate-paperclip-to-aiforge.py --dry-run
    python scripts/runtime/migrate-paperclip-to-aiforge.py --status todo in_progress in_review blocked
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import psycopg
from psycopg.rows import dict_row


PAPERCLIP_DSN = os.environ.get(
    "PAPERCLIP_DSN",
    "postgresql://paperclip:paperclip@127.0.0.1:54329/paperclip",
)
AIFORGE_DSN = os.environ.get(
    "AIFORGE_DSN", "postgresql://manikanta@127.0.0.1:5432/aiforge",
)


# Paperclip agent UUID → our role name.
ROLE_MAP = {
    "35760e2f-4cef-4013-9aff-d93592b5f71e": "architect",
    "28b8c064-bfcf-44e1-9e91-e37c39e0097c": "sr_developer",
    "e0502e94-0608-4fb9-9afa-b70d8dbf014a": "developer",
    "7e1a0654-ec02-4dc7-ac6c-d5f97cb5ff7c": "fact_extract",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="migrate every Paperclip issue")
    ap.add_argument("--status", nargs="+",
                    default=["todo", "in_progress", "in_review", "blocked"])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with psycopg.connect(PAPERCLIP_DSN, row_factory=dict_row) as pc:
        with pc.cursor() as cur:
            if args.all:
                cur.execute("SELECT * FROM issues ORDER BY issue_number")
            else:
                cur.execute(
                    "SELECT * FROM issues WHERE status = ANY(%s) ORDER BY issue_number",
                    (args.status,),
                )
            rows = list(cur.fetchall())
    print(f"[migrate] candidate issues: {len(rows)}")
    if args.dry_run:
        for r in rows[:10]:
            print(f"  {r['identifier']:<10} {r['status']:<12} {r['title'][:70]}")
        print("(dry-run — no writes)")
        return 0

    # We're batching into aiforge; preserve identifier (ONE-<n>) but keep
    # parent_id relationships by identifier first, then fix up.
    with psycopg.connect(AIFORGE_DSN) as af:
        af.autocommit = False
        with af.cursor() as cur:
            # Pass 1: insert all rows with parent_id = NULL
            inserted: dict[str, int] = {}  # identifier -> aiforge.id
            for r in rows:
                role = ROLE_MAP.get(r.get("assignee_agent_id") or "")
                meta = {
                    "migrated_from": "paperclip",
                    "paperclip_id": str(r["id"]),
                    "original_status": r["status"],
                }
                cur.execute(
                    """
                    INSERT INTO tickets
                      (identifier, title, body, status, priority, assignee_role,
                       project, labels, metadata, created_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                    ON CONFLICT (identifier) DO NOTHING
                    RETURNING id
                    """,
                    (
                        r["identifier"],
                        r["title"] or "",
                        r.get("description") or "",
                        r["status"],
                        r.get("priority") or "medium",
                        role,
                        None,  # project mapping not essential
                        [],
                        json.dumps(meta),
                        r["created_at"],
                    ),
                )
                row = cur.fetchone()
                if row:
                    inserted[r["identifier"]] = row[0]

            # Pass 2: wire up parent_id
            for r in rows:
                if not r.get("parent_id"):
                    continue
                # resolve parent identifier via its paperclip uuid
                cur.execute(
                    "SELECT identifier FROM tickets WHERE metadata->>'paperclip_id'=%s",
                    (str(r["parent_id"]),),
                )
                row = cur.fetchone()
                if not row:
                    continue
                parent_ident = row[0]
                cur.execute(
                    "UPDATE tickets SET parent_id = "
                    "(SELECT id FROM tickets WHERE identifier=%s) "
                    "WHERE identifier=%s",
                    (parent_ident, r["identifier"]),
                )

            # Bump ticket_counter so future ONE-<n> > max(existing n)
            cur.execute(
                "SELECT COALESCE(MAX(CAST(regexp_replace(identifier,'ONE-','') AS bigint)),0) FROM tickets"
            )
            max_n = cur.fetchone()[0]
            cur.execute(
                "UPDATE ticket_counter SET next_n = GREATEST(next_n, %s + 1)",
                (max_n,),
            )
        af.commit()

    print(f"[migrate] inserted {len(inserted)} new ticket rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
