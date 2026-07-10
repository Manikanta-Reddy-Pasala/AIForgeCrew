"""CodeGraph tool — query a pre-indexed code knowledge graph (symbols, callers,
callees, impact) via the local `codegraph` binary (SQLite, zero-Docker).

Gives agents EXPLICIT code relations without scanning files: "who calls X",
"what does changing X impact", "find the symbol for 'handle login'". The graph
is built + auto-synced by `codegraph init` in the repo; this tool just reads it.

Config (env):
  AIFORGE_CODEGRAPH_BIN   path to the codegraph binary (default: on PATH)
  AIFORGE_CODEGRAPH_PATH  the INDEXED repo root (holds .codegraph/) — defaults
                          to the request-context repo root, then cwd. Needed
                          because the Doer runs in a git WORKTREE that has no
                          .codegraph of its own.

Soft-error: returns ``{"ok": bool, ...}``, never raises.
"""
from __future__ import annotations

import os
import shutil
import subprocess

_TIMEOUT_S = 30
_CAP = 12000


def _bin() -> str | None:
    b = os.environ.get("AIFORGE_CODEGRAPH_BIN")
    if b and os.path.exists(b):
        return b
    return shutil.which("codegraph") or (
        os.path.expanduser("~/.npm-global/bin/codegraph")
        if os.path.exists(os.path.expanduser("~/.npm-global/bin/codegraph"))
        else None)


def _repo(cwd: str | None) -> str:
    p = os.environ.get("AIFORGE_CODEGRAPH_PATH")
    if p:
        return _main_repo(p)
    try:
        from aiforge_core.runtime import request_context
        r = request_context.get_repo_root() or cwd or "."
    except Exception:  # noqa: BLE001
        r = cwd or "."
    return _main_repo(r)


def _main_repo(path: str) -> str:
    """Resolve to the repo that holds the ``.codegraph`` index. A ticket Doer
    runs inside ``<repo>/.aiforge-worktrees/<TICKET>`` (or ``.worktrees/…``),
    which has no index of its own — strip back to the parent repo so the
    codegraph_* queries hit the real index instead of an empty worktree."""
    for marker in (os.sep + ".aiforge-worktrees" + os.sep,
                   os.sep + ".worktrees" + os.sep):
        if marker in path:
            return path.split(marker, 1)[0]
    return path


def available() -> bool:
    return _bin() is not None


def _run(args: list[str], cwd: str | None) -> dict:
    exe = _bin()
    if not exe:
        return {"ok": False, "error": "codegraph binary not found "
                "(install: npm i -g @colbymchenry/codegraph)"}
    cmd = [exe, *args, "--path", _repo(cwd)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "codegraph timed out"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    out = (p.stdout or "").strip()
    if p.returncode != 0 and not out:
        return {"ok": False, "error": (p.stderr or "codegraph failed")[:800]}
    return {"ok": True, "result": out[:_CAP]}


def codegraph_query(args: dict, cwd: str | None = None) -> dict:
    """Find symbols in the codebase (semantic + name). Required: ``query``."""
    q = str(args.get("query") or args.get("search") or "").strip()
    if not q:
        return {"ok": False, "error": "missing 'query'"}
    return _run(["query", q], cwd)


def codegraph_callers(args: dict, cwd: str | None = None) -> dict:
    """Functions/methods that CALL a symbol. Required: ``symbol``."""
    s = str(args.get("symbol") or "").strip()
    if not s:
        return {"ok": False, "error": "missing 'symbol'"}
    return _run(["callers", s], cwd)


def codegraph_callees(args: dict, cwd: str | None = None) -> dict:
    """Functions/methods a symbol CALLS. Required: ``symbol``."""
    s = str(args.get("symbol") or "").strip()
    if not s:
        return {"ok": False, "error": "missing 'symbol'"}
    return _run(["callees", s], cwd)


def codegraph_impact(args: dict, cwd: str | None = None) -> dict:
    """What code is AFFECTED by changing a symbol (blast radius). Required:
    ``symbol``. Use before an edit to find everything that must stay in sync."""
    s = str(args.get("symbol") or "").strip()
    if not s:
        return {"ok": False, "error": "missing 'symbol'"}
    return _run(["impact", s], cwd)


def codegraph_explore(args: dict, cwd: str | None = None) -> dict:
    """Explore an area — relevant symbols + their source for a natural-language
    query. Required: ``query``."""
    q = str(args.get("query") or "").strip()
    if not q:
        return {"ok": False, "error": "missing 'query'"}
    return _run(["explore", q], cwd)


__all__ = ["available", "codegraph_query", "codegraph_callers",
           "codegraph_callees", "codegraph_impact", "codegraph_explore"]
