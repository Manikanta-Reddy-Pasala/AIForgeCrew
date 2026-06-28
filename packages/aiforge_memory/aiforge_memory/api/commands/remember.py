"""``aiforge-memory remember`` — record a memory node.

Routes to the right ``upsert_*`` writer based on ``--type``. Decision
nodes carry ``rationale`` + ``supersedes_id``; observations get
embedded for vector recall (sidecar offline → ``embedded=False`` in
the response, no error).
"""
from __future__ import annotations

import argparse
import json

from aiforge_memory.features.memory import store as memory_writer

from ._csv import split_csv
from ._driver import driver
from ._embed import embed_text


def run(args: argparse.Namespace) -> int:
    drv = driver()
    refs = split_csv(args.refs)
    tags = split_csv(args.tags)
    if args.type == "decision":
        out = memory_writer.upsert_decision(
            drv, repo=args.repo,
            title=args.title or args.text[:80],
            body=args.text, rationale=args.why or "",
            status=args.status, author=args.author, session_id=args.session,
            tags=tags, refs=refs,
            supersedes_id=args.supersedes,
        )
    elif args.type == "observation":
        vec = embed_text(args.text) if not args.no_embed else None
        out = memory_writer.upsert_observation(
            drv, repo=args.repo, text=args.text,
            kind=args.kind or "note", author=args.author,
            session_id=args.session, tags=tags, refs=refs,
            embed_vec=vec,
        )
        out["embedded"] = vec is not None
    elif args.type == "note":
        out = memory_writer.upsert_note(
            drv, repo=args.repo,
            title=args.title or args.text[:80],
            body=args.text, author=args.author,
            tags=tags, refs=refs,
        )
    elif args.type == "doc":
        out = memory_writer.upsert_doc(
            drv, repo=args.repo,
            title=args.title or args.text[:80],
            body=args.text, url=args.url or "",
            source_kind=args.kind or "web", refs=refs,
        )
    else:
        print(json.dumps({"error": "unknown type"}))
        return 2
    print(json.dumps(out, indent=2))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("remember", help="Record a memory node")
    p.add_argument("repo")
    p.add_argument("--type",
                   choices=["decision", "observation", "note", "doc"],
                   required=True)
    p.add_argument("--text", required=True, help="Body / observation text")
    p.add_argument("--title", help="Title (decision/note/doc)")
    p.add_argument("--why", help="Rationale (decision)")
    p.add_argument("--status", default="active",
                   help="Decision status: active|superseded|rejected")
    p.add_argument("--kind", help="Observation kind / Doc source_kind")
    p.add_argument("--author", default="",
                   help="Author identifier (agent / user)")
    p.add_argument("--session", default="",
                   help="Session id for grouping memories")
    p.add_argument("--tags", help="comma-separated tags")
    p.add_argument("--refs",
                   help="comma-separated Symbol fqnames or File paths")
    p.add_argument("--supersedes",
                   help="Decision id this one supersedes")
    p.add_argument("--url", help="Doc source URL")
    p.add_argument("--no-embed", action="store_true",
                   help="Skip embedding even for observation")
    p.set_defaults(func=run)


__all__ = ["run", "register"]
