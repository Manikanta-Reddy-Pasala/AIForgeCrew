"""ONE canonical "which repo am I" resolver.

Five resolvers had grown across the codebase on three different bases
(git-toplevel / workspace-dir / REPO_ROOT-parse) with different fallbacks and
sentinels — so the same session could file memory under one key and recall it
under another (the recurring "written here, read there" bug). This is the single
name resolver they all delegate to.

``repo_name(cwd)`` = the GIT-TOPLEVEL basename (so a subdir resolves the same
repo key as the root), falling back to the raw ``cwd`` basename, then
``AIFORGE_AFM_REPO``, then ``sentinel``.
"""
from __future__ import annotations

import os
import subprocess

_GIT_TOPLEVEL_CACHE: "dict[str, str | None]" = {}


def git_toplevel(cwd: "str | None") -> "str | None":
    """``git rev-parse --show-toplevel`` for ``cwd`` — cached, soft-fails to
    None outside a work tree."""
    if not cwd:
        return None
    key = str(cwd)
    if key in _GIT_TOPLEVEL_CACHE:
        return _GIT_TOPLEVEL_CACHE[key]
    top: "str | None" = None
    try:
        out = subprocess.run(
            ["git", "-C", key, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=3)
        if out.returncode == 0:
            top = out.stdout.strip() or None
    except Exception:  # noqa: BLE001 — resolution must never break on git
        top = None
    _GIT_TOPLEVEL_CACHE[key] = top
    return top


def repo_name(cwd: "str | None", *, sentinel: str = "repo") -> str:
    """Canonical repo key: git-toplevel basename → cwd basename →
    ``AIFORGE_AFM_REPO`` → ``sentinel``."""
    base_path = git_toplevel(cwd) or cwd
    base = os.path.basename(os.path.normpath(str(base_path))).strip() if base_path else ""
    return normalize_repo(base) or os.environ.get("AIFORGE_AFM_REPO") or sentinel


def normalize_repo(name: "str | None") -> str:
    """Strip a trailing display suffix like `" (Python)"` / `" (Java Spring)"`
    from a repo key. Memory SOURCES carry a language-annotated display name
    ('requests (Python)'), but the chat/recall path keys by the bare git
    basename ('requests') — so writes filed under the annotated name were never
    found by recall. Normalising both sides to the bare name fixes the mismatch.
    Idempotent; a bare name passes through unchanged."""
    import re
    n = (name or "").strip()
    return re.sub(r"\s*\([^)]*\)\s*$", "", n).strip() or n


__all__ = ["git_toplevel", "repo_name", "normalize_repo"]
