"""Maintenance CLI — cron-friendly entry points.

Subcommands (all idempotent, all best-effort, all log+exit-0 on
soft errors so a failed timer firing doesn't cascade):

    aiforge-maint memory decay       → memory.decay.run()
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


def _cmd_memory_decay(args) -> int:
    from aiforge_core.memory import decay
    out = decay.run()
    print(json.dumps({"cmd": "memory.decay", **out}))
    return 0


def _cmd_memory_reembed(args) -> int:
    """Backfill vectors for notes written while the embedder was unavailable
    (stored with the ``[]`` sentinel) and re-embed after a model/backend switch."""
    from aiforge_core.memory import sqlite_memory
    out = sqlite_memory.reembed_all()
    print(json.dumps({"cmd": "memory.reembed", **out}))
    return 0


def _cmd_index_merkle(args) -> int:
    from aiforge_core.indexing import merkle
    root = merkle.build(args.path)
    print(json.dumps({"cmd": "index.merkle", "path": args.path,
                      "root": root}))
    return 0


def _cmd_docs_ingest(args) -> int:
    from aiforge_core.indexing import docs_index
    n = docs_index.ingest(args.library, args.urls,
                          chunk_chars=args.chunk_chars)
    print(json.dumps({"cmd": "docs.ingest", "library": args.library,
                      "added": n}))
    return 0


def _cmd_memory_migrate_okr(args) -> int:
    from aiforge_core.memory import md_store
    print(json.dumps(md_store.migrate_to_okr()))
    return 0


def _cmd_repo_notes(args) -> int:
    from aiforge_core.indexing.repo_notes import generate_repo_notes
    try:
        path = generate_repo_notes(args.repo)
    except Exception as exc:
        print(json.dumps({"repo": args.repo, "error": str(exc)[:300]}))
        # Exit NON-zero on the failure path: this printed {"error": ...} and
        # then exited 0, so a shell (`... && next-step`) or a CI step read a
        # failed note generation as success.
        return 1
    print(json.dumps({"repo": args.repo, "wrote": path}))
    return 0


def _cmd_cost_snapshot(args) -> int:
    # `cost` lives under observability, not runtime. The wrong path made this
    # command dead on arrival: main()'s catch-all printed the ImportError and
    # returned 0 (cron-friendly), so it looked like a successful run that just
    # happened to report an error.
    from aiforge_core.observability import cost
    print(json.dumps(cost.snapshot(args.ticket)))
    return 0


def main(argv: list[str] | None = None) -> int:
    # Sourced HERE, not at import. It used to run at module scope, which meant
    # merely IMPORTING this module injected the operator's personal
    # ~/.aiforge/runtime.env into os.environ for the whole process — a global
    # side effect from an import. It surfaced as 16 unrelated triage tests
    # failing in the full suite while passing alone: that file carries
    # AIFORGE_FORCE_FULL_PIPELINE=1, so importing the maintenance CLI anywhere
    # in the session forced every triage decision to the full pipeline.
    # Only the CLI entry point wants this (cron jobs without systemd's
    # EnvironmentFile), so only the CLI entry point does it.
    _load_runtime_env()
    p = argparse.ArgumentParser(prog="aiforge-maint")
    sub = p.add_subparsers(dest="cmd", required=True)

    mem = sub.add_parser("memory")
    mem_sub = mem.add_subparsers(dest="action", required=True)
    mem_sub.add_parser("decay").set_defaults(func=_cmd_memory_decay)
    mem_sub.add_parser("reembed",
                       help="backfill vectors for notes stored without an "
                            "embedding (model was unavailable at write time)"
                       ).set_defaults(func=_cmd_memory_reembed)
    mem_sub.add_parser("migrate-okr",
                       help="rewrite legacy compacted-*.md knowledge briefs "
                            "into the standard OKR envelope (idempotent)"
                       ).set_defaults(func=_cmd_memory_migrate_okr)

    idx = sub.add_parser("index")
    idx_sub = idx.add_subparsers(dest="action", required=True)
    mk = idx_sub.add_parser("merkle")
    mk.add_argument("path")
    mk.set_defaults(func=_cmd_index_merkle)

    rn = sub.add_parser("repo")
    rn_sub = rn.add_subparsers(dest="action", required=True)
    nt = rn_sub.add_parser("notes",
                           help="auto-generate <repo>/.aiforge/REPO_NOTES.md "
                                "from worktree scan (controllers, services, "
                                "kafka/nats, collections, build cmds)")
    nt.add_argument("repo")
    nt.set_defaults(func=_cmd_repo_notes)

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
        # Exit NON-zero. This used to return 0 "cron-friendly — log error but
        # exit 0", and that is precisely what hid two dead commands: `codemem
        # ingest` imported a function that did not exist and `cost snapshot`
        # imported the wrong module, and BOTH printed their ImportError and
        # then reported success. A cron job that mails on failure stayed
        # silent; `aiforge-maint ... && next-step` ran next-step.
        #
        # The error line is still printed, so nothing a cron job used to see is
        # lost — only the lie about the exit code is. This matches the same
        # decision already made for `repo notes`, which returns 1 on failure
        # for exactly this reason.
        print(json.dumps({"cmd": getattr(args, "cmd", "?"),
                          "error": str(exc)[:400]}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
