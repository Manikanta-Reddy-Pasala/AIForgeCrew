"""Maintenance CLI — cron-friendly entry points.

Subcommands (all idempotent, all best-effort, all log+exit-0 on
soft errors so a failed timer firing doesn't cascade):

    aiforge-maint memory decay       → memory.decay.run()
    aiforge-maint memory mine        → memory.pattern_miner.run()
    aiforge-maint index symbols [--repo NAME]  → index.symbol_embed.backfill()
    aiforge-maint index merkle <path>          → index.merkle.build()
    aiforge-maint docs ingest <library> <url>  → index.docs_index.ingest()
    aiforge-maint cost snapshot                → runtime.cost.snapshot() print

Wire as console_script ``aiforge-maint`` in pyproject.toml. KISS:
flat argparse, no plugins, JSON-line output for log scrapers.
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def _load_runtime_env() -> None:
    """Source ``~/.aiforge/runtime.env`` so cron jobs without
    systemd's EnvironmentFile see the same DSN/keys the live
    service uses.

    KISS: shell-style ``KEY=VALUE`` parsing, no quoting magic.
    Existing env values WIN — explicit wrapper exports stay
    authoritative.
    """
    path = os.path.expanduser(
        os.environ.get("AIFORGE_RUNTIME_ENV", "~/.aiforge/runtime.env"),
    )
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


_load_runtime_env()


def _cmd_memory_decay(args) -> int:
    from aiforge_core.memory import decay
    out = decay.run()
    print(json.dumps({"cmd": "memory.decay", **out}))
    return 0


def _cmd_memory_mine(args) -> int:
    from aiforge_core.memory import pattern_miner
    out = pattern_miner.run()
    print(json.dumps({"cmd": "memory.mine", **out}))
    return 0


def _cmd_index_symbols(args) -> int:
    from aiforge_core.index import symbol_embed
    out = symbol_embed.backfill(repo=args.repo, batch=args.batch)
    print(json.dumps({"cmd": "index.symbols", "repo": args.repo, **out}))
    return 0


def _cmd_index_merkle(args) -> int:
    from aiforge_core.index import merkle
    root = merkle.build(args.path)
    print(json.dumps({"cmd": "index.merkle", "path": args.path,
                      "root": root}))
    return 0


def _cmd_docs_ingest(args) -> int:
    from aiforge_core.index import docs_index
    n = docs_index.ingest(args.library, args.urls,
                          chunk_chars=args.chunk_chars)
    print(json.dumps({"cmd": "docs.ingest", "library": args.library,
                      "added": n}))
    return 0


def _cmd_cost_snapshot(args) -> int:
    from aiforge_core.runtime import cost
    print(json.dumps(cost.snapshot(args.ticket)))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="aiforge-maint")
    sub = p.add_subparsers(dest="cmd", required=True)

    mem = sub.add_parser("memory")
    mem_sub = mem.add_subparsers(dest="action", required=True)
    mem_sub.add_parser("decay").set_defaults(func=_cmd_memory_decay)
    mem_sub.add_parser("mine").set_defaults(func=_cmd_memory_mine)

    idx = sub.add_parser("index")
    idx_sub = idx.add_subparsers(dest="action", required=True)
    sym = idx_sub.add_parser("symbols")
    sym.add_argument("--repo", default=None)
    sym.add_argument("--batch", type=int, default=200)
    sym.set_defaults(func=_cmd_index_symbols)
    mk = idx_sub.add_parser("merkle")
    mk.add_argument("path")
    mk.set_defaults(func=_cmd_index_merkle)

    docs = sub.add_parser("docs")
    docs_sub = docs.add_subparsers(dest="action", required=True)
    ing = docs_sub.add_parser("ingest")
    ing.add_argument("library")
    ing.add_argument("urls", nargs="+")
    ing.add_argument("--chunk-chars", type=int, default=1500,
                     dest="chunk_chars")
    ing.set_defaults(func=_cmd_docs_ingest)

    co = sub.add_parser("cost")
    co_sub = co.add_subparsers(dest="action", required=True)
    snap = co_sub.add_parser("snapshot")
    snap.add_argument("--ticket", default=None)
    snap.set_defaults(func=_cmd_cost_snapshot)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:
        print(json.dumps({"cmd": getattr(args, "cmd", "?"),
                          "error": str(exc)[:400]}))
        return 0  # cron-friendly — log error but exit 0


if __name__ == "__main__":
    sys.exit(main())
