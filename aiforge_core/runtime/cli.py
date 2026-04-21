"""`aiforge ticket ...` CLI — file, list, show, update tickets without a UI.

Subcommands:
    ticket create --title "..." --body "..." --assignee sr_developer [--priority high]
    ticket list [--role sr_developer] [--status todo,in_progress]
    ticket show <ONE-<n>>
    ticket comment <ONE-<n>> --body "..."
    ticket status <ONE-<n>> --status done
    ticket tick <role>                # manual single tick
"""
from __future__ import annotations

import argparse
import json
import sys

from . import tickets
from .orchestrator import tick as orchestrator_tick


def _cmd_create(args) -> int:
    t = tickets.create(
        title=args.title, body=args.body or "",
        assignee_role=args.assignee, priority=args.priority,
        project=args.project, labels=args.labels or [],
    )
    print(f"{t.identifier}  id={t.id}  assignee={t.assignee_role}")
    return 0


def _cmd_list(args) -> int:
    import psycopg
    from psycopg.rows import dict_row
    from .config import AIFORGE_DSN
    q = "SELECT identifier, status, priority, assignee_role, title FROM tickets"
    clauses, params = [], []
    if args.role:
        clauses.append("assignee_role = %s"); params.append(args.role)
    if args.status:
        statuses = [s.strip() for s in args.status.split(",")]
        clauses.append("status = ANY(%s)"); params.append(statuses)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY created_at DESC LIMIT %s"; params.append(args.limit)
    with psycopg.connect(AIFORGE_DSN, row_factory=dict_row) as c, c.cursor() as cur:
        cur.execute(q, params)
        rows = cur.fetchall()
    for r in rows:
        print(f"{r['identifier']:<8} {r['status']:<12} {r['priority']:<6} "
              f"{(r['assignee_role'] or '-'):<14} {r['title'][:80]}")
    return 0


def _cmd_show(args) -> int:
    t = tickets.get(args.identifier)
    if not t:
        print(f"no such ticket: {args.identifier}", file=sys.stderr); return 2
    print(f"{t.identifier}  {t.status}  {t.priority}  assignee={t.assignee_role}")
    print(f"title: {t.title}")
    print(f"branch: {t.branch or '-'}")
    print(f"parent: {t.parent_id or '-'}")
    print(f"labels: {t.labels}")
    print("--- body ---")
    print(t.body)
    print("--- events (last 30) ---")
    for e in tickets.comments(t.id, limit=30):
        ts = e["created_at"].strftime("%H:%M:%S") if e.get("created_at") else "?"
        print(f"[{ts}] [{e['agent_role'] or '-'}] ({e['kind']}) {(e['body'] or '')[:400]}")
    return 0


def _cmd_comment(args) -> int:
    t = tickets.get(args.identifier)
    if not t:
        print(f"no such ticket: {args.identifier}", file=sys.stderr); return 2
    eid = tickets.add_comment(t.id, "human", args.body)
    print(f"comment event id={eid}")
    return 0


def _cmd_status(args) -> int:
    t = tickets.get(args.identifier)
    if not t:
        print(f"no such ticket: {args.identifier}", file=sys.stderr); return 2
    tickets.update_status(t.id, args.status, role="human")
    print(f"{t.identifier} → {args.status}")
    return 0


def _cmd_tick(args) -> int:
    return orchestrator_tick(args.role)


def main() -> int:
    p = argparse.ArgumentParser(prog="aiforge ticket")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create")
    c.add_argument("--title", required=True)
    c.add_argument("--body")
    c.add_argument("--assignee", choices=["architect", "sr_developer",
                   "developer", "fact_extract"], required=True)
    c.add_argument("--priority", default="medium",
                   choices=["low", "medium", "high", "urgent"])
    c.add_argument("--project")
    c.add_argument("--labels", nargs="*")
    c.set_defaults(func=_cmd_create)

    l = sub.add_parser("list")
    l.add_argument("--role")
    l.add_argument("--status")
    l.add_argument("--limit", type=int, default=30)
    l.set_defaults(func=_cmd_list)

    s = sub.add_parser("show")
    s.add_argument("identifier")
    s.set_defaults(func=_cmd_show)

    cc = sub.add_parser("comment")
    cc.add_argument("identifier")
    cc.add_argument("--body", required=True)
    cc.set_defaults(func=_cmd_comment)

    st = sub.add_parser("status")
    st.add_argument("identifier")
    st.add_argument("--status", required=True,
                    choices=["todo", "in_progress", "in_review",
                             "done", "blocked", "cancelled"])
    st.set_defaults(func=_cmd_status)

    tk = sub.add_parser("tick")
    tk.add_argument("role", choices=["architect", "sr_developer",
                                     "developer", "fact_extract"])
    tk.set_defaults(func=_cmd_tick)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
