"""``aiforge-memory health`` — probe Neo4j + LM + embed + rerank sidecars."""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict

from aiforge_memory.ops import health as ops_health


def run(args: argparse.Namespace) -> int:
    report = ops_health.check_all()
    ops_health.write_snapshot(report)
    if args.table:
        print(ops_health.render_table(report))
    else:
        print(json.dumps({
            "ts": report.ts,
            "overall_ok": report.overall_ok,
            "checks": [asdict(c) for c in report.checks],
        }, indent=2))
    return 0 if report.overall_ok else 1


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "health",
        help="Probe Neo4j + LM + embed + rerank sidecars",
    )
    p.add_argument("--table", action="store_true")
    p.set_defaults(func=run)


__all__ = ["run", "register"]
