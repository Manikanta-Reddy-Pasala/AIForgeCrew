"""``aiforge-memory stats`` — print Repo node summary."""
from __future__ import annotations

import argparse
import json

from ._driver import driver


def run(args: argparse.Namespace) -> int:
    drv = driver()
    with drv.session() as s:
        row = s.run(
            "MATCH (r:Repo {name:$n}) RETURN r", n=args.repo
        ).single()
    if not row:
        print(json.dumps({"error": "repo_not_found", "repo": args.repo}))
        return 1
    r = dict(row["r"])
    if "last_indexed_at" in r and r["last_indexed_at"] is not None:
        r["last_indexed_at"] = str(r["last_indexed_at"])
    runbook = r.pop("runbook_md", "") or ""
    conventions = r.pop("conventions_md", "") or ""
    r["runbook_md_chars"] = len(runbook)
    r["conventions_md_chars"] = len(conventions)
    print(json.dumps(r, indent=2, default=str))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("stats", help="Print Repo node summary")
    p.add_argument("repo")
    p.set_defaults(func=run)


__all__ = ["run", "register"]
