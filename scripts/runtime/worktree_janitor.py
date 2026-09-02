#!/usr/bin/env python3
"""Remove ``.aiforge-worktrees/<TICKET>`` dirs whose ticket has finished.

Replaces the old shell version, which queried POSTGRES directly for the
status. This build is SQLite-only (``tickets.backend_factory``), so that
query returned nothing on every run: the script demanded AIFORGE_PG_PASSWORD,
exited 1 when it was unset, and — had it run — would have read an empty status
for every ticket and kept every worktree forever. Reading the status through
the ticket store instead means this follows the backend wherever it goes.

TERMINAL means the ticket will not be worked again: done, cancelled, qa_failed.
``blocked`` is NOT terminal — a blocked ticket is resumed by an operator, and
the old script deleted its worktree, throwing away work in progress. An unknown
ticket is kept, never deleted: the safe answer when we cannot prove it finished.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

# Ticket states after which the worktree is dead weight. Mirrors
# ``store._COMPLETED_STATUSES``; kept explicit so a change there is a visible
# change here rather than a silent behaviour swap.
TERMINAL = frozenset({"done", "cancelled", "qa_failed"})

_WORKTREE_DIR = ".aiforge-worktrees"


def _root() -> Path:
    return Path(os.environ.get("AIFORGE_WORKTREE_ROOT")
                or (Path.home() / "codeRepo")).expanduser()


def _ticket_status(identifier: str) -> "str | None":
    """The ticket's status, or None when it is unknown/unreadable."""
    try:
        from aiforge_core.tickets import store
        t = store.get(identifier)
        return getattr(t, "status", None) if t else None
    except Exception as exc:  # noqa: BLE001 — a store hiccup must keep the tree
        print(f"  ! status lookup failed for {identifier}: {exc}")
        return None


def _git(args: list[str], cwd: Path) -> "subprocess.CompletedProcess | None":
    try:
        return subprocess.run(["git", *args], cwd=str(cwd), text=True,
                              capture_output=True, timeout=120)
    except Exception as exc:  # noqa: BLE001
        print(f"  ! git {' '.join(args)} failed in {cwd}: {exc}")
        return None


def _worktrees(root: Path) -> "list[tuple[Path, Path]]":
    """``(repo_dir, worktree_dir)`` for every checkout under the root."""
    out: list[tuple[Path, Path]] = []
    try:
        repos = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError as exc:
        print(f"! cannot read {root}: {exc}")
        return out
    for repo in repos:
        holder = repo / _WORKTREE_DIR
        if not holder.is_dir():
            continue
        try:
            out.extend((repo, wt) for wt in sorted(holder.iterdir())
                       if wt.is_dir())
        except OSError as exc:  # noqa: PERF203 — one unreadable repo, not all
            print(f"! cannot read {holder}: {exc}")
    return out


def _sweep_one(repo: Path, wt: Path, dry_run: bool) -> str:
    """Decide and act on ONE worktree. Returns the counter to bump:
    ``keep`` / ``removed`` / ``failed``."""
    ident = wt.name
    status = _ticket_status(ident)
    label = f"[{repo.name}/{ident}] status={status or '?'}"
    if status not in TERMINAL:
        print(f"{label} -> keep")
        return "kept"
    if dry_run:
        print(f"{label} -> WOULD remove")
        return "removed"
    res = _git(["worktree", "remove", "--force", str(wt)], repo)
    if res is not None and res.returncode == 0:
        print(f"{label} -> removed")
        return "removed"
    err = (res.stderr or "").strip().splitlines()[-1:] if res else []
    print(f"{label} -> REMOVE FAILED {err[0] if err else ''}")
    return "failed"


def sweep(root: Path, *, dry_run: bool = False) -> dict:
    """Remove the finished tickets' worktrees under ``root``."""
    counts = {"removed": 0, "kept": 0, "failed": 0}
    pairs = _worktrees(root)
    for repo, wt in pairs:
        counts[_sweep_one(repo, wt, dry_run)] += 1
    # Drop refs to worktrees already gone from disk (including the ones just
    # removed), once per repo.
    if not dry_run:
        for repo in dict.fromkeys(r for r, _ in pairs):
            _git(["worktree", "prune"], repo)
    print(f"removed={counts['removed']} kept={counts['kept']} "
          f"failed={counts['failed']}")
    return counts


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=None,
                    help="worktree root (default: $AIFORGE_WORKTREE_ROOT or "
                         "~/codeRepo)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be removed, touch nothing")
    args = ap.parse_args(argv)
    root = Path(args.root).expanduser() if args.root else _root()
    if not root.is_dir():
        print(f"! worktree root {root} does not exist — nothing to do")
        return 0
    res = sweep(root, dry_run=args.dry_run)
    # A removal that failed is worth a non-zero exit (systemd marks the unit
    # failed and the log says which one) — a clean sweep with nothing to do is
    # a success, not a failure.
    return 1 if res["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
