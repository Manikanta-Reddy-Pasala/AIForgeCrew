"""``aiforge-memory link`` + ``link-list`` — cross-repo CALLS_REPO edges.

Both subcommands live here because they share the same store + only
differ in direction (compute vs read).
"""
from __future__ import annotations

import argparse
import json

from aiforge_memory.features.link import extract as link_extract
from aiforge_memory.features.link import store as link_writer

from ._csv import split_csv
from ._driver import driver


def run_link(args: argparse.Namespace) -> int:
    repos = split_csv(args.repos)
    if len(repos) < 2:
        print(json.dumps({"error": "need at least 2 repos via --repos"}))
        return 2
    drv = driver()
    counts = link_extract.run(drv, repos=repos,
                              min_confidence=args.min_confidence)
    print(json.dumps(counts, indent=2))
    return 0


def run_link_list(args: argparse.Namespace) -> int:
    drv = driver()
    rows = link_writer.list_edges(drv, repo=args.repo)
    print(json.dumps({"edges": rows}, indent=2))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p_link = sub.add_parser("link",
                            help="Compute cross-repo CALLS_REPO edges")
    p_link.add_argument("--repos", required=True,
                        help="comma-separated repo names")
    p_link.add_argument("--min-confidence", type=float, default=0.0)
    p_link.set_defaults(func=run_link)

    p_list = sub.add_parser("link-list", help="List CALLS_REPO edges")
    p_list.add_argument("--repo", help="filter to edges touching this repo")
    p_list.set_defaults(func=run_link_list)


__all__ = ["run_link", "run_link_list", "register"]
