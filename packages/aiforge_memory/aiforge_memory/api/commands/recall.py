"""``aiforge-memory recall`` — vector recall over Observations.

Embeds the query via the bge-m3 sidecar; when the sidecar is offline
the command exits 2 with an explicit error so callers don't silently
get an empty result set.
"""
from __future__ import annotations

import argparse
import json

from aiforge_memory.features.memory import store as memory_writer

from ._driver import driver
from ._embed import embed_text


def run(args: argparse.Namespace) -> int:
    drv = driver()
    vec = embed_text(args.query)
    if vec is None:
        print(json.dumps({
            "error": "embed sidecar unreachable", "query": args.query,
        }))
        return 2
    rows = memory_writer.recall_observations(
        drv, repo=args.repo, query_vec=vec, k=args.k,
    )
    print(json.dumps({"repo": args.repo, "results": rows}, indent=2))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("recall", help="Vector recall over Observations")
    p.add_argument("repo")
    p.add_argument("--query", required=True)
    p.add_argument("--k", type=int, default=10)
    p.set_defaults(func=run)


__all__ = ["run", "register"]
