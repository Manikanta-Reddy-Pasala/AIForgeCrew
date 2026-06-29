"""Workspace path sandbox.

Every tool call that touches the filesystem resolves its path through
:func:`resolve_inside_root` so a hallucinated ``..`` traversal can't
scribble outside the operator's chosen workspace.

The root comes from ``AIFORGE_REPO_ROOT`` (default
``$HOME/aiforge_workspace``). Created on first use — caller never has
to mkdir it.
"""
from __future__ import annotations

import contextvars
import os
from pathlib import Path

# Per-execution root override. The chat ReAct agent runs each turn against a
# per-session ``cwd`` (not the env ``AIFORGE_REPO_ROOT`` the ADK pipeline uses),
# so it sets this contextvar to its cwd before dispatching the shared tools
# (editor/lsp/typecheck/format/test). A contextvar is thread/async-isolated, so
# concurrent chat sessions don't clobber each other.
_ROOT_OVERRIDE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "aiforge_root_override", default=None)


def set_root_override(path: "str | os.PathLike | None") -> None:
    """Point :func:`root` at ``path`` for this execution context (None clears)."""
    _ROOT_OVERRIDE.set(str(path) if path else None)


def root() -> Path:
    """Return the absolute, existing repo root (context override > env > default)."""
    raw = _ROOT_OVERRIDE.get() or os.environ.get(
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


__all__ = ["root", "resolve_inside_root", "set_root_override"]
