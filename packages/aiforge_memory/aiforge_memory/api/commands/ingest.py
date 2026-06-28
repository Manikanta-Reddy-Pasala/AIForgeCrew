"""``aiforge-memory ingest`` — Stage 1+2 ingest of a repo.

Owns the orchestration of: schema apply → state DB migrate → flow runner
(or delta runner with cold-start fallback) → status JSON. Heavy lifting
lives in :mod:`aiforge_memory.features.flow.runner` and
:mod:`aiforge_memory.features.delta.extract`.
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, is_dataclass
from pathlib import Path

from aiforge_memory.core import neo4j as schema
from aiforge_memory.core import state as sdb
from aiforge_memory.features.delta import extract as delta
from aiforge_memory.features.flow import runner as flow

from ._driver import driver


def run(args: argparse.Namespace) -> int:
    from aiforge_memory.config import RepoConfig

    repo_path = args.path or os.getcwd()
    cfg = RepoConfig.load(repo_path, name=args.repo)
    cfg.apply_to_env()  # so legacy modules pick up overrides

    drv = driver()
    schema.apply(drv)
    state = sdb.open_db()
    sdb.migrate(state)

    if args.delta:
        res = delta.ingest_delta(
            repo_name=cfg.name, repo_path=cfg.path,
            driver=drv, state_conn=state,
            skip_summaries=cfg.skip_summaries,
            skip_chunks=cfg.skip_chunks,
            use_lsp=args.lsp,
        )
        if res.status == "cold_start_required":
            # Auto-fall-through to full ingest on first run.
            res = flow.ingest_repo(
                repo_name=cfg.name, repo_path=cfg.path,
                driver=drv, state_conn=state, force=False,
                skip_services=cfg.skip_services,
                skip_symbols=cfg.skip_symbols,
                skip_summaries=cfg.skip_summaries,
                skip_chunks=cfg.skip_chunks,
                use_lsp=args.lsp,
            )
    else:
        res = flow.ingest_repo(
            repo_name=cfg.name,
            repo_path=cfg.path,
            driver=drv,
            state_conn=state,
            force=args.force,
            skip_services=cfg.skip_services,
            skip_symbols=cfg.skip_symbols,
            skip_summaries=cfg.skip_summaries,
            skip_chunks=cfg.skip_chunks,
            use_lsp=args.lsp,
        )

    if is_dataclass(res):
        base = asdict(res)
    else:
        # Defensive: tests may stub ingest with a plain object.
        base = {k: getattr(res, k) for k in (
            "status", "pack_sha", "repo",
        ) if hasattr(res, k)}
    payload = {
        **base,
        "config_loaded_from": str(
            (Path(repo_path) / ".aiforge" / "codemem.yaml").resolve()
        ),
    }
    print(json.dumps(payload, default=str, indent=2))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("ingest", help="Stage 1+2 ingest of a repo")
    p.add_argument("repo", help="Logical repo name (becomes Repo.name)")
    p.add_argument("--path", help="Repo dir; defaults to CWD")
    p.add_argument("--force", action="store_true",
                   help="Re-run even if pack_sha matches")
    p.add_argument("--delta", action="store_true",
                   help="Re-index only files changed since last ingest")
    p.add_argument("--lsp", action="store_true",
                   help="Layer LSP-confirmed CALLS on top of tree-sitter "
                        "heuristic (per-language adapter required on PATH)")
    p.set_defaults(func=run)


__all__ = ["run", "register"]
