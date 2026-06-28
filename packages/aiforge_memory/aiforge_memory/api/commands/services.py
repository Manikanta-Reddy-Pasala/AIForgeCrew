"""``aiforge-memory services`` — list services for a repo."""
from __future__ import annotations

import argparse
import json

from ._driver import driver


def run(args: argparse.Namespace) -> int:
    drv = driver()
    with drv.session() as s:
        rows = list(s.run(
            "MATCH (r:Repo {name:$n})-[:OWNS_SERVICE]->(s:Service) "
            "OPTIONAL MATCH (s)-[:CONTAINS_FILE]->(f:File_v2) "
            "WITH s, count(f) AS file_count "
            "RETURN s.name AS name, s.role AS role, s.port AS port, "
            "       s.source AS source, s.tech_stack AS tech_stack, "
            "       file_count, s.description AS description "
            "ORDER BY s.name", n=args.repo,
        ))
    services = [dict(r) for r in rows]
    if not services:
        print(json.dumps({"repo": args.repo, "services": []}))
        return 0 if args.allow_empty else 1
    print(json.dumps({"repo": args.repo, "services": services}, indent=2))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("services", help="List services for a repo")
    p.add_argument("repo")
    p.add_argument("--allow-empty", action="store_true",
                   help="exit 0 even when no services found")
    p.set_defaults(func=run)


__all__ = ["run", "register"]
