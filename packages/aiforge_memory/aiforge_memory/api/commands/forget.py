"""``aiforge-memory forget`` — hard-delete a memory node by id."""
from __future__ import annotations

import argparse
import json

from aiforge_memory.features.memory import store as memory_writer

from ._driver import driver

_LABEL_MAP = {
    "decision":    "Decision_v2",
    "observation": "Observation_v2",
    "note":        "Note_v2",
    "doc":         "Doc_v2",
}


def run(args: argparse.Namespace) -> int:
    label = _LABEL_MAP.get(args.type)
    if not label:
        print(json.dumps({"error": "unknown type"}))
        return 2
    drv = driver()
    res = memory_writer.forget(drv, repo=args.repo,
                               node_id=args.id, label=label)
    print(json.dumps(res, indent=2))
    return 0 if res.get("deleted") else 1


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("forget", help="Hard-delete a memory node by id")
    p.add_argument("repo")
    p.add_argument("--id", required=True)
    p.add_argument("--type", choices=list(_LABEL_MAP.keys()), required=True)
    p.set_defaults(func=run)


__all__ = ["run", "register"]
