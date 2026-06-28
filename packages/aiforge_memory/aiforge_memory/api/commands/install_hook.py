"""``aiforge-memory install-hook`` — install delta-ingest git hooks."""
from __future__ import annotations

import argparse
import json
import os

from aiforge_memory.features.delta import extract as delta


def run(args: argparse.Namespace) -> int:
    repo_path = args.path or os.getcwd()
    try:
        commit_hook = delta.install_post_commit_hook(repo_path, args.repo)
        merge_hook = delta.install_post_merge_hook(repo_path, args.repo)
    except FileNotFoundError as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    print(json.dumps({
        "installed_post_commit": str(commit_hook),
        "installed_post_merge": str(merge_hook),
    }, indent=2))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "install-hook",
        help="Install git post-commit + post-merge hooks for delta ingest",
    )
    p.add_argument("repo")
    p.add_argument("--path", help="Repo dir; defaults to CWD")
    p.set_defaults(func=run)


__all__ = ["run", "register"]
