"""``aiforge-memory decay`` — archive stale, never-reused memory facts."""
from __future__ import annotations

import argparse
import json

from aiforge_memory.features.memory import decay as memory_decay

from ._driver import driver


def run(args: argparse.Namespace) -> int:
    drv = driver()
    try:
        res = memory_decay.run_decay(drv, max_age_days=args.max_age_days)
    finally:
        drv.close()
    print(json.dumps(res, indent=2))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "decay",
        help="Archive Observation/Decision facts with seen_count<=1 "
             "older than --max-age-days (status='archived', recoverable)",
    )
    p.add_argument("--max-age-days", type=int, default=30,
                   help="minimum age before archival (default 30)")
    p.set_defaults(func=run)


__all__ = ["run", "register"]
