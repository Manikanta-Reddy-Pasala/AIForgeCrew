"""Request-scoped runtime context (concurrency-safe).

The multi-threaded API (deploy-anywhere multi-project mode) serves concurrent
chats on DIFFERENT repos. The old code stashed the active repo root, workspace
dir, and delegation depth in ``os.environ`` — process-global state that two
concurrent runs clobber (chat on /repoA sets ``AIFORGE_REPO_ROOT=/repoA``,
chat on /repoB overwrites it, and the first run's tools then read /repoB).

This module replaces that with :class:`contextvars.ContextVar` values, which
auto-isolate per thread / async-task: a freshly started thread gets its own
context (default values), and :func:`asyncio.to_thread` COPIES the current
context into the worker thread — so an ADK tool call dispatched through it
observes the value the pipeline set. (``loop.run_in_executor(None, ...)`` does
NOT copy context, so callers that hop an executor must read the getter in the
async task and pass the value down — see ``hooks.py``.)

Design mirrors :mod:`aiforge_core.runtime.sandbox` (``_ROOT_OVERRIDE``):

* setters return the contextvar Token so a ``finally`` can reset it (critical
  on pooled/reused threads where a bare ``set()`` would leak into the next
  task);
* getters read the CONTEXTVAR first, then fall back to ``os.environ`` — so the
  single-shot subprocess graph-runner (which sets env for child processes) and
  any non-context-propagating executor still work, and single-threaded
  behaviour is byte-for-byte identical to the old env-only path;
* getters are soft-fail / None-safe: they never raise.
"""
from __future__ import annotations

import contextvars
import os

_REPO_ROOT_ENV = "AIFORGE_REPO_ROOT"
_WORKSPACE_DIR_ENV = "AIFORGE_WORKSPACE_DIR"

_REPO_ROOT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "aiforge_repo_root", default=None)
_WORKSPACE_DIR: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "aiforge_workspace_dir", default=None)
# Delegation recursion counter — purely internal request state (no env
# fallback; the OLD env var was only ever written by delegation itself).
_DELEGATION_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar(
    "aiforge_delegation_depth", default=0)


# ─────────────────────────── repo root ────────────────────────────

def set_repo_root(value: "str | os.PathLike | None"):
    """Point the current context's repo root at ``value`` (None clears).
    Returns a Token for :func:`reset_repo_root`."""
    return _REPO_ROOT.set(str(value) if value else None)


def reset_repo_root(token) -> None:
    try:
        _REPO_ROOT.reset(token)
    except Exception:  # noqa: BLE001 — wrong-context token, never fatal
        _REPO_ROOT.set(None)


def get_repo_root() -> "str | None":
    """Active repo root: contextvar first, then ``AIFORGE_REPO_ROOT`` env,
    else None. Never raises."""
    try:
        v = _REPO_ROOT.get()
    except Exception:  # noqa: BLE001
        v = None
    return v or os.environ.get(_REPO_ROOT_ENV)


# ───────────────────────── workspace dir ──────────────────────────

def set_workspace_dir(value: "str | os.PathLike | None"):
    """Point the current context's workspace dir (path-jail root) at ``value``
    (None clears). Returns a Token for :func:`reset_workspace_dir`."""
    return _WORKSPACE_DIR.set(str(value) if value else None)


def reset_workspace_dir(token) -> None:
    try:
        _WORKSPACE_DIR.reset(token)
    except Exception:  # noqa: BLE001
        _WORKSPACE_DIR.set(None)


def get_workspace_dir() -> "str | None":
    """Active workspace dir: contextvar first, then ``AIFORGE_WORKSPACE_DIR``
    env, else None. Never raises."""
    try:
        v = _WORKSPACE_DIR.get()
    except Exception:  # noqa: BLE001
        v = None
    return v or os.environ.get(_WORKSPACE_DIR_ENV)


# ─────────────────────── delegation depth ─────────────────────────

def get_delegation_depth() -> int:
    """Current delegation recursion depth for this context (0 by default)."""
    try:
        return int(_DELEGATION_DEPTH.get())
    except Exception:  # noqa: BLE001
        return 0


def enter_delegation():
    """Increment the depth for this context; returns a Token for
    :func:`reset_delegation` (call it in a finally)."""
    return _DELEGATION_DEPTH.set(get_delegation_depth() + 1)


def reset_delegation(token) -> None:
    try:
        _DELEGATION_DEPTH.reset(token)
    except Exception:  # noqa: BLE001
        _DELEGATION_DEPTH.set(0)


__all__ = [
    "set_repo_root", "reset_repo_root", "get_repo_root",
    "set_workspace_dir", "reset_workspace_dir", "get_workspace_dir",
    "get_delegation_depth", "enter_delegation", "reset_delegation",
]
