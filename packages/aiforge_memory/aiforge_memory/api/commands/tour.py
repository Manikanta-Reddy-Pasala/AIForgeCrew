"""``aiforge-memory tour`` — build + store an ordered onboarding tour."""
from __future__ import annotations

import argparse

from aiforge_memory.features.tour import extract as tx
from aiforge_memory.features.tour import store as tstore

from ._driver import driver


def run(args: argparse.Namespace) -> int:
    drv = driver()
    tour = tx.build_tour(drv, repo=args.repo, domain=args.domain,
                         naming=not args.no_naming)
    if not tour.stops:
        print(f"# Tour: {args.repo}\n\n(no symbols — ingest the repo first)")
        return 0 if args.allow_empty else 1
    tstore.upsert_tour(drv, repo=args.repo, tour=tour)
    tstore.prune_tours(drv, repo=args.repo, keep_names=[tour.name])
    md = tx.render_md(tour)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(md)
        print(f"wrote {len(tour.stops)} stops -> {args.out}")
    else:
        print(md)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("tour", help="Build an ordered learning tour for a repo")
    p.add_argument("repo")
    p.add_argument("--domain", default=None,
                   help="restrict the tour to one :Domain's symbols")
    p.add_argument("--out", default=None, help="write tour.md to this path")
    p.add_argument("--no-naming", action="store_true",
                   help="skip the optional LLM narration (doc-line notes)")
    p.add_argument("--allow-empty", action="store_true")
    p.set_defaults(func=run)


__all__ = ["run", "register"]
