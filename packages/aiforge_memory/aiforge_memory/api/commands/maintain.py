"""``aiforge-memory maintain`` — sleep-time consolidation (frontier gap #4).

One idempotent maintenance pass to run on a timer (e.g. nightly / on idle):
decay stale never-reused facts, then digest cold observations per repo into
compact summaries. This is the "background consolidation while idle" job
(Letta sleep-time compute) — the pieces existed (decay + digest) but only
ran by hand; this bundles them behind one scheduled entrypoint.

Wire to a timer to make it automatic, e.g. a systemd timer or cron:
    aiforge-memory maintain --all-repos
"""
from __future__ import annotations

import argparse
import json
import time

from aiforge_memory.features.memory import decay as _decay
from aiforge_memory.features.memory import digest as _digest

from ._driver import driver


def _all_repos(drv) -> list[str]:
    with drv.session() as s:
        return [r["n"] for r in s.run("MATCH (r:Repo) RETURN r.name AS n")
                if r["n"]]


def run(args: argparse.Namespace) -> int:
    drv = driver()
    out: dict = {}
    try:
        out["decay"] = _decay.run_decay(drv, max_age_days=args.max_age_days)
        repos: list[str] = []
        if args.repo:
            repos = [args.repo]
        elif args.all_repos:
            repos = _all_repos(drv)
        digests: dict = {}
        now = time.time()
        for repo in repos:
            try:
                digests[repo] = _digest.run_digest(drv, repo=repo, now=now)
            except Exception as exc:  # noqa: BLE001 — one repo must not abort all
                digests[repo] = {"error": str(exc)}
        out["digest"] = digests
    finally:
        drv.close()
    print(json.dumps(out, indent=2))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "maintain",
        help="Sleep-time consolidation: decay stale facts + digest cold "
             "observations. Run on a timer (nightly/idle) for automatic "
             "background memory upkeep.",
    )
    p.add_argument("--max-age-days", type=int, default=30,
                   help="decay archival age threshold (default 30)")
    p.add_argument("--repo", default="",
                   help="digest only this repo (default: none unless "
                        "--all-repos)")
    p.add_argument("--all-repos", action="store_true",
                   help="digest every Repo node in the graph")
    p.set_defaults(func=run)


__all__ = ["run", "register"]
