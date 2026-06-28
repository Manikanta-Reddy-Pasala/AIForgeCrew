"""``aiforge-memory list-memory`` — list memory nodes for a repo."""
from __future__ import annotations

import argparse
import json

from aiforge_memory.features.memory import store as memory_writer

from ._driver import driver
from .forget import _LABEL_MAP  # share the same type-to-label mapping


def run(args: argparse.Namespace) -> int:
    label = _LABEL_MAP.get(args.type) if args.type else None
    drv = driver()
    rows = memory_writer.list_memory(
        drv, repo=args.repo, label=label, limit=args.limit,
    )
    print(json.dumps({"repo": args.repo, "memory": rows}, indent=2))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("list-memory", help="List memory nodes for a repo")
    p.add_argument("repo")
    p.add_argument("--type", choices=list(_LABEL_MAP.keys()))
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=run)


__all__ = ["run", "register"]
