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
    for stem in (".aiforge-worktrees", ".worktrees"):
        # match on both native + forward-slash separators (portable / Windows)
        for marker in (os.sep + stem + os.sep, "/" + stem + "/"):
            if marker in path:
                return path.split(marker, 1)[0]
    return path


def available() -> bool:
    """The codegraph BINARY is installed (says nothing about any repo index)."""
    return _bin() is not None


def indexed(cwd: str | None = None) -> bool:
    """A queryable ``.codegraph`` index exists for the resolved (main) repo.
    ``available()`` only proves the globally-installed binary — this proves the
    THIS-repo index the tools actually need."""
    try:
        r = _repo(cwd)
        return bool(r) and os.path.isdir(os.path.join(r, ".codegraph"))
    except Exception:  # noqa: BLE001
        return False


def _disabled() -> bool:
    return os.environ.get("AIFORGE_CODEGRAPH_DISABLE", "0").strip().lower() in (
        "1", "true", "yes", "on")


def _ticket_opts_out() -> bool:
    """Per-ticket A/B opt-out — ``ticket.metadata['codegraph']`` is
    False / 0 / 'false'|'0'|'off'|'no' (read via AIFORGE_CURRENT_TICKET)."""
    ident = os.environ.get("AIFORGE_CURRENT_TICKET", "")
    if not ident:
        return False
    try:
        from aiforge_core.tickets import store
        t = store.get(ident)
        val = ((getattr(t, "metadata", None) or {}) if t else {}).get("codegraph")
    except Exception:  # noqa: BLE001 — never gate on a store hiccup
        return False
    if val is False or val == 0:          # bool False OR numeric 0
        return True
    return isinstance(val, str) and val.strip().lower() in (
        "false", "0", "off", "no")


def enabled_for_run(cwd: str | None = None) -> bool:
    """THE single gate deciding whether codegraph is advertised AND enforced on
    a run. All three call sites (Doer seed mandate, chat tool catalog, Doer tool
    filter) use this so they can never disagree: binary present AND a real index
    exists for the repo AND not env-disabled AND not opted out per-ticket."""
    return (available() and indexed(cwd) and not _disabled()
            and not _ticket_opts_out())


import threading as _threading  # noqa: E402
import time as _time  # noqa: E402
from collections import defaultdict as _defaultdict  # noqa: E402

# PER-REPO build locks (not one global) — a first-time build of repo X must not
# block an unrelated first-time build of repo Y across sessions.
_LOCKS: "dict[str, _threading.Lock]" = _defaultdict(_threading.Lock)
_LOCKS_GUARD = _threading.Lock()
# Negative cache: repo → monotonic ts of last FAILED/timed-out build, so a repo
# that can't index within the budget isn't re-attempted (blocking!) every turn.
_FAILED: "dict[str, float]" = {}


def _lock_for(repo: str) -> "_threading.Lock":
    with _LOCKS_GUARD:
        return _LOCKS[repo]


def _retry_cooldown_s() -> int:
    try:
        return max(60, int(os.environ.get(
            "AIFORGE_CODEGRAPH_RETRY_COOLDOWN_S", "3600")))
    except (TypeError, ValueError):
        return 3600


def _autobuild_enabled() -> bool:
    return os.environ.get("AIFORGE_CODEGRAPH_AUTOBUILD", "1").strip().lower() \
        not in ("0", "false", "no", "off")


def _init_cmd() -> str:
    """The binary subcommand that BUILDS the index (``codegraph init`` by
    default — overridable if the installed binary spells it differently)."""
    return os.environ.get("AIFORGE_CODEGRAPH_INIT_CMD", "init").strip() or "init"


def _build_timeout_s() -> int:
    try:
        return max(10, int(os.environ.get("AIFORGE_CODEGRAPH_BUILD_TIMEOUT_S",
                                          "180")))
    except (TypeError, ValueError):
        return 180


def ensure_indexed(cwd: str | None = None, *, timeout_s: int | None = None) -> bool:
    """Blocking, bounded first-time build: if the resolved repo has no
    ``.codegraph`` index yet, run ``codegraph init --path <repo>`` ONCE (deduped
    by a process lock) so the codegraph tools become available for THIS folder.
    Returns whether the index exists afterwards.

    No-ops (returns ``indexed()``) when the binary is missing, codegraph is
    env-disabled, or autobuild is turned off (AIFORGE_CODEGRAPH_AUTOBUILD=0).
    Never raises — a build failure just leaves the tools unavailable, same as
    before. Callers invoke this at turn start so the FIRST turn on a freshly
    pinned repo can use codegraph (the user chose blocking-first-time)."""
    if not _autobuild_enabled() or _disabled() or not available():
        return indexed(cwd)
    if indexed(cwd):
        return True
    repo = _repo(cwd)
    if not repo or not os.path.isdir(repo):
        return False
    # Negative cache: a repo that failed/timed out must NOT re-trigger the
    # (blocking) build every turn — that hung chat forever on a repo too big to
    # index in the budget. Skip re-attempts for a cooldown window.
    ts = _FAILED.get(repo)
    if ts is not None and (_time.monotonic() - ts) < _retry_cooldown_s():
        return False
    with _lock_for(repo):            # PER-REPO lock (not one global)
        if indexed(cwd):            # built by another thread while we waited
            return True
        # Re-check the negative cache INSIDE the lock — a thread that queued
        # while another built-and-failed must honor that fresh failure, not
        # re-run the full blocking build.
        ts2 = _FAILED.get(repo)
        if ts2 is not None and (_time.monotonic() - ts2) < _retry_cooldown_s():
            return False
        exe = _bin()
        if not exe:
            return False
        try:
            # NOTE: `init` takes a POSITIONAL path (`codegraph init <path>`) —
            # unlike the query subcommands, which use `-p/--path`. Passing
            # `--path` here makes the binary reject it ("unknown option
            # '--path'") so autobuild silently failed and every un-preindexed
            # repo stayed "CodeGraph not initialized".
            p = subprocess.run([exe, _init_cmd(), repo],
                               capture_output=True, text=True,
                               timeout=timeout_s or _build_timeout_s())
        except Exception:  # noqa: BLE001 — timeout / spawn failure
            _FAILED[repo] = _time.monotonic()
            return False
        # A non-zero exit (wrong subcommand, disk full, partial write) can still
        # leave a stub .codegraph dir — require BOTH a clean exit and a real
        # index before trusting it, else negative-cache and stay unavailable.
        if p.returncode != 0 or not indexed(cwd):
            _FAILED[repo] = _time.monotonic()
            return False
        _FAILED.pop(repo, None)
        return True


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
    err = (p.stderr or "").strip()
    if p.returncode != 0:
        # non-zero exit: surface stderr instead of silently trusting stdout. If
        # there IS stdout (a partial/degraded result), return it but attach the
        # diagnostic so the caller isn't misled into treating it as authoritative.
        if not out:
            return {"ok": False, "error": (err or "codegraph failed")[:800]}
        return {"ok": True, "result": out[:_CAP], "warning": err[:400]}
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
