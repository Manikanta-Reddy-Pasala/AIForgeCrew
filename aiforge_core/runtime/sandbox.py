"""Workspace path sandbox.

Every tool call that touches the filesystem resolves its path through
:func:`resolve_inside_root` so a hallucinated ``..`` traversal can't
scribble outside the operator's chosen workspace.

The root comes from ``AIFORGE_REPO_ROOT`` (default
``$HOME/aiforge_workspace``). Created on first use — caller never has
to mkdir it.
"""
from __future__ import annotations

import os
from pathlib import Path


def root() -> Path:
    """Return the absolute, existing repo root."""
    raw = os.environ.get(
        "AIFORGE_REPO_ROOT",
        str(Path.home() / "aiforge_workspace"),
    )
    p = Path(raw).expanduser().resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def resolve_inside_root(rel: str) -> Path:
    """Resolve ``rel`` against the repo root, reject path-traversal.

    Raises :class:`PermissionError` when the resolved path falls outside
    the root. Empty / ``"."`` paths return the root itself.
    """
    r = root()
    target = (r / rel).resolve()
    if r not in target.parents and target != r:
        raise PermissionError(
            f"path {rel!r} resolves outside AIFORGE_REPO_ROOT={r}"
        )
    return target


__all__ = ["root", "resolve_inside_root"]
