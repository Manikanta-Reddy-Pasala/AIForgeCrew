"""``aiforge-memory review`` — graph completeness audit (coverage gaps)."""
from __future__ import annotations

import argparse
import json
import time

from aiforge_memory.features.review import extract as rx
from aiforge_memory.features.review import store as rstore

from ._driver import driver


def run(args: argparse.Namespace) -> int:
    drv = driver()
    report = rx.review_graph(drv, repo=args.repo, naming=not args.no_naming)
    if args.store:
        run_id = str(int(time.time()))
        rstore.upsert_review(drv, repo=args.repo, report=report, run_id=run_id)
    if args.json:
        print(json.dumps({
            "repo": report.repo,
            "totals": report.totals,
            "findings": [{"kind": f.kind, "severity": f.severity,
                          "count": f.count, "sample": f.sample,
                          "message": f.message} for f in report.findings],
            "note": report.note,
        }, indent=2))
    else:
        print(rx.render_md(report))
    # exit 1 when high-severity gaps exist (useful as a CI gate)
    return 1 if any(f.severity == "high" for f in report.findings) else 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("review", help="Audit graph completeness for a repo")
    p.add_argument("repo")
    p.add_argument("--json", action="store_true")
    p.add_argument("--store", action="store_true",
                   help="persist :ReviewFinding nodes for trend tracking")
    p.add_argument("--no-naming", action="store_true",
                   help="skip the optional LLM commentary")
    p.set_defaults(func=run)


__all__ = ["run", "register"]
