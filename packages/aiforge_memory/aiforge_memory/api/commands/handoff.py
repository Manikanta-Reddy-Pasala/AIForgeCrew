"""``aiforge-memory handoff`` — portable JSON snapshot of a repo's
memory (gap #5).

Emits the recent decisions / observations / notes for a repo as a single
JSON document, so a new agent session can be seeded with "here's what the
last run learned" instead of starting cold. The analog of agentmemory's
``/handoff`` skill, scoped to a code repo rather than a chat session.
"""
from __future__ import annotations

import argparse
import json

from aiforge_memory.features.memory import store as memory_writer

from ._driver import driver

_BUCKET = {
    "Decision_v2": "decisions",
    "Observation_v2": "observations",
    "Note_v2": "notes",
    "Doc_v2": "docs",
}


def _build_handoff(repo: str, rows: list[dict]) -> dict:
    snap: dict = {
        "repo": repo,
        "count": len(rows),
        "decisions": [],
        "observations": [],
        "notes": [],
        "docs": [],
    }
    for r in rows:
        bucket = _BUCKET.get(r.get("label") or "")
        if bucket is None:
            continue
        snap[bucket].append({
            "id": r.get("id"),
            "title": r.get("title") or "",
            "text": r.get("text") or "",
            "kind": r.get("kind") or "",
            "created_at": r.get("created_at") or "",
        })
    return snap


def run(args: argparse.Namespace) -> int:
    drv = driver()
    rows = memory_writer.list_memory(drv, repo=args.repo, limit=args.limit)
    print(json.dumps(_build_handoff(args.repo, rows), indent=2))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "handoff", help="Portable JSON memory snapshot for a repo")
    p.add_argument("repo")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=run)


__all__ = ["run", "register"]
