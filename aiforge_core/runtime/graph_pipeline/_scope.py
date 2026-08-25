"""Repo-worktree scope helpers — glob matching + ticket repo-root resolution.

Split out of the former single-file ``graph_pipeline.py`` (grouped by
concern). Leaf module — no cross-group dependency. No behaviour change.
"""
from __future__ import annotations

import os


_SCOPE_SKIP_DIRS = {".git", "node_modules", ".venv", "target", "dist", "build",
                    ".aiforge-worktrees", "__pycache__"}


def _iter_scannable_rel_paths(base):
    """Yield each file under ``base`` as a base-relative Path, skipping
    vendor/build dirs. The skip check is on the path RELATIVE to base — not
    p.parts. The ticket worktree lives UNDER .aiforge-worktrees/, so an absolute
    p.parts check matched '.aiforge-worktrees' on EVERY file and skipped the
    whole tree → "matches nothing" → scope wrongly cleared → empty pass
    (ONE-163/164)."""
    for p in base.rglob("*"):
        if not p.is_file():
            continue
        try:
            rel_path = p.relative_to(base)
        except ValueError:
            continue
        if any(part in _SCOPE_SKIP_DIRS for part in rel_path.parts):
            continue
        yield rel_path


def _globs_match_any_repo_file(globs: "list[str]", repo_root: "str | None" = None,
                              max_files: int = 4000) -> bool:
    """True if at least one file under the ticket's repo (worktree) matches any
    of ``globs``. Used to detect a bad plan whose scope allowlist matches
    nothing in THIS repo. Soft-fails to True (don't clear on a probe error —
    only clear when we're SURE nothing matches)."""
    root = repo_root or _repo_root_for_scope()
    if not root or not globs:
        return True
    try:
        from aiforge_core.runtime import scope_guard
        from pathlib import Path
        base = Path(root)
        n = 0
        for rel_path in _iter_scannable_rel_paths(base):
            n += 1
            if n > max_files:
                return True                # too big to fully scan → don't clear
            if scope_guard._matches_any(str(rel_path), globs):
                return True
        return False
    except Exception:  # noqa: BLE001 — a probe failure must not clear the scope
        return True


def _repo_root_for_scope() -> str:
    """The ticket's actual worktree root for scope/rule matching. The runner
    sets it on request_context per run; AIFORGE_REPO_ROOT is a generic
    workspace fallback (was the ONLY source before — so the glob check scanned
    the wrong, near-empty dir and cleared every plan's scope)."""
    try:
        from aiforge_core.runtime import request_context
        r = request_context.get_repo_root()
        if r:
            return str(r)
    except Exception:  # noqa: BLE001
        pass
    return os.environ.get("AIFORGE_REPO_ROOT") or ""
