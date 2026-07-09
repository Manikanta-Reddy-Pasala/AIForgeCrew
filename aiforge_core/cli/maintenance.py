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
    from aiforge_core.indexing import symbol_embed
    out = symbol_embed.backfill(repo=args.repo, batch=args.batch)
    print(json.dumps({"cmd": "index.symbols", "repo": args.repo, **out}))
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


def _cmd_index_purge_noise(args) -> int:
    """Drop pre-existing :Symbol/:Chunk/:File/:Memory nodes whose
    file_path matches the shared noise filter (target/, build/,
    node_modules/, .pyc/.class/.jar/...). One-shot cleanup; the
    indexers already skip these going forward."""
    from aiforge_core.indexing.noise import EXCLUDE_DIR_TOKENS, PURGE_CYPHER
    try:
        from aiforge_core.memory.rag.neo4j_memory import _get_driver
    except Exception as exc:
        print(json.dumps({"error": f"neo4j driver: {exc}"})); return 0
    tokens = list(EXCLUDE_DIR_TOKENS)
    if args.dry_run:
        print(json.dumps({
            "dry_run": True, "tokens": tokens,
            "cypher": PURGE_CYPHER.strip(),
        }))
        return 0
    out: list[dict] = []
    with _get_driver().session() as s:
        for r in s.run(PURGE_CYPHER, tokens=tokens):
            out.append({"token": r["tok"], "purged": int(r["purged"])})
    print(json.dumps({
        "purged_total": sum(r["purged"] for r in out),
        "by_token": out,
    }))
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
        return 0
    print(json.dumps({"repo": args.repo, "wrote": path}))
    return 0


def _cmd_repo_learn(args) -> int:
    from aiforge_core.indexing.repo_learn import learn_repo
    kinds = [k.strip() for k in (args.kinds or "").split(",") if k.strip()]
    try:
        result = learn_repo(
            args.repo, kinds=kinds or None,
            limit=args.limit, sleep_s=args.sleep_s,
        )
    except Exception as exc:
        result = {"repo": args.repo, "error": str(exc)[:400]}
    print(json.dumps(result))
    return 0


def _cmd_cost_snapshot(args) -> int:
    from aiforge_core.runtime import cost
    print(json.dumps(cost.snapshot(args.ticket)))
    return 0


def _cmd_codemem_ingest(args) -> int:
    from aiforge_memory.api.cli import _cmd_ingest as _ing
    return _ing(args)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="aiforge-maint")
    sub = p.add_subparsers(dest="cmd", required=True)

    mem = sub.add_parser("memory")
    mem_sub = mem.add_subparsers(dest="action", required=True)
    mem_sub.add_parser("decay").set_defaults(func=_cmd_memory_decay)
    mem_sub.add_parser("mine").set_defaults(func=_cmd_memory_mine)
    mem_sub.add_parser("migrate-okr",
                       help="rewrite legacy compacted-*.md knowledge briefs "
                            "into the standard OKR envelope (idempotent)"
                       ).set_defaults(func=_cmd_memory_migrate_okr)

    idx = sub.add_parser("index")
    idx_sub = idx.add_subparsers(dest="action", required=True)
    sym = idx_sub.add_parser("symbols")
    sym.add_argument("--repo", default=None)
    sym.add_argument("--batch", type=int, default=200)
    sym.set_defaults(func=_cmd_index_symbols)
    mk = idx_sub.add_parser("merkle")
    mk.add_argument("path")
    mk.set_defaults(func=_cmd_index_merkle)
    pn = idx_sub.add_parser("purge-noise",
                            help="drop pre-indexed noise from Neo4j "
                                 "(target/, build/, .pyc, etc.)")
    pn.add_argument("--dry-run", action="store_true",
                    help="print tokens + Cypher, do not delete")
    pn.set_defaults(func=_cmd_index_purge_noise)

    rn = sub.add_parser("repo")
    rn_sub = rn.add_subparsers(dest="action", required=True)
    nt = rn_sub.add_parser("notes",
                           help="auto-generate <repo>/.aiforge/REPO_NOTES.md "
                                "from worktree scan (controllers, services, "
                                "kafka/nats, collections, build cmds)")
    nt.add_argument("repo")
    nt.set_defaults(func=_cmd_repo_notes)

    lr = rn_sub.add_parser("learn",
                           help="LLM-summarise every controller/service/"
                                "repository file in the repo and persist "
                                "to T2 memory (idempotent via sha1)")
    lr.add_argument("repo")
    lr.add_argument("--kinds", default="controller,service,service_impl,repository",
                    help="comma-separated subset of kinds to process")
    lr.add_argument("--limit", type=int, default=None,
                    help="max files per run (chunk via cron)")
    lr.add_argument("--sleep", type=float, default=0.0, dest="sleep_s",
                    help="seconds between LLM calls (rate limit)")
    lr.set_defaults(func=_cmd_repo_learn)

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

    cm = sub.add_parser("codemem", help="codemem operator commands")
    cm_sub = cm.add_subparsers(dest="action", required=True)
    cm_ing = cm_sub.add_parser("ingest", help="Stage 1+2 ingest")
    cm_ing.add_argument("repo")
    cm_ing.add_argument("--path")
    cm_ing.add_argument("--force", action="store_true")
    cm_ing.set_defaults(func=_cmd_codemem_ingest)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:
        print(json.dumps({"cmd": getattr(args, "cmd", "?"),
                          "error": str(exc)[:400]}))
        return 0  # cron-friendly — log error but exit 0


if __name__ == "__main__":
    sys.exit(main())
