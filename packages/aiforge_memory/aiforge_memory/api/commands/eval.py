"""``aiforge-memory eval`` — run NL probe eval against a repo.

Heavy lifting in :mod:`aiforge_memory.features.eval.harness`. The
``--fail-under`` knob lets CI gate on Recall@5 regression.
"""
from __future__ import annotations

import argparse

from ._driver import driver


def run(args: argparse.Namespace) -> int:
    from aiforge_memory.features.eval import harness as ev

    drv = driver()
    report = ev.run_eval(
        probes_path=args.probes, driver=drv,
        repo=args.repo, token_budget=args.budget,
    )
    if args.table:
        print(ev.render_table(report))
    else:
        print(ev.report_to_json(report))
    if args.fail_under is not None and report.recall_at_5 < args.fail_under:
        return 1
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("eval", help="Run NL probe eval against a repo")
    p.add_argument("repo", nargs="?", default=None,
                   help="overrides probes.yaml repo when given")
    p.add_argument("--probes", required=True, help="path to probes yaml")
    p.add_argument("--budget", type=int, default=4000,
                   help="ContextBundle token budget")
    p.add_argument("--table", action="store_true", help="markdown table")
    p.add_argument("--fail-under", type=float, default=None,
                   help="exit 1 if Recall@5 below this threshold")
    p.set_defaults(func=run)


__all__ = ["run", "register"]
