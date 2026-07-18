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
        if not r:
            return False
        d = os.path.join(r, ".codegraph")
        # Require a POPULATED index, not a bare directory — a build that timed
        # out / aborted (esp. on the "nolock" path where we can't safely remove
        # it) leaves an empty or partial .codegraph dir; trusting mere existence
        # made every later turn short-circuit onto a corrupt index forever.
        # `with` so the ScandirIterator's dir fd is closed on the hot (populated)
        # path where any() short-circuits before exhausting it.
        if not os.path.isdir(d):
            return False
        with os.scandir(d) as it:
            return any(it)
    except Exception:  # noqa: BLE001
        return False


def _build_lock_path(repo: str) -> str:
    """Lock-file path for a repo — kept OUTSIDE the repo (temp dir, keyed by a
    hash of the repo's CANONICAL path) so it can NEVER be staged into the Doer's
    PR the way a ``<repo>/.codegraph.build.lock`` would. Uses ``realpath`` +
    ``normcase`` (not ``abspath``) so two processes reaching the SAME physical
    repo via a symlink, a bind mount, or different CWDs (the ``.`` fallback) map
    to the SAME lock file — else the flock wouldn't actually exclude them."""
    import hashlib
    import tempfile
    try:
        canon = os.path.normcase(os.path.realpath(repo))
    except Exception:  # noqa: BLE001
        canon = os.path.normcase(os.path.abspath(repo))
    h = hashlib.sha1(canon.encode("utf-8", "replace")).hexdigest()[:16]
    return os.path.join(tempfile.gettempdir(), f"aiforge-codegraph-{h}.lock")


def _acquire_build_lock(repo: str):
    """Non-blocking OS file lock so two PROCESSES (per-ticket Doers run as
    separate processes, all resolving to the same parent repo) don't run
    ``codegraph init`` on the same SQLite index concurrently — a half-written
    index. Returns the held file object, the string ``"nolock"`` on a platform
    without ``fcntl`` (proceed on the thread-lock alone), or ``None`` when
    another process holds it (caller skips the duplicate build). Never raises.
    The lock file lives OUTSIDE the repo so it can't leak into a PR."""
    try:
        import fcntl
    except Exception:  # noqa: BLE001 — non-POSIX: rely on the per-repo thread lock
        return "nolock"
    try:
        f = open(_build_lock_path(repo), "w")
    except OSError:
        return "nolock"
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except OSError:                      # another process is building
        f.close()
        return None
    except Exception:  # noqa: BLE001
        f.close()
        return "nolock"


def _remove_partial_index(repo: str) -> None:
    """Best-effort remove a half-written ``.codegraph`` left by a failed/timed-out
    build. Safe: ensure_indexed only builds when the repo was NOT already
    indexed, so any index present after a failed build is a stub from that
    attempt, not a good prior index. Never raises."""
    try:
        import shutil
        d = os.path.join(repo, ".codegraph")
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


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
    ``.codegraph`` index yet, run ``codegraph init <repo>`` (POSITIONAL path)
    ONCE (deduped by a per-repo lock) so the codegraph tools become available
    for THIS folder.
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
    # Canonicalize ONCE so the per-repo thread lock, the negative cache AND the
    # cross-process flock all key on the SAME path — else two spellings of one
    # repo (the "." fallback, a symlink) would get separate _FAILED / _LOCKS
    # entries and the flock's canonical guarantee wouldn't match them.
    try:
        repo = os.path.normcase(os.path.realpath(repo))
    except Exception:  # noqa: BLE001
        pass
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
        # Cross-PROCESS guard: another process may already be building this same
        # repo's index (concurrent tickets). None = contended → skip the
        # duplicate build (the other process will finish; next turn re-checks).
        _fl = _acquire_build_lock(repo)
        if _fl is None:
            return False
        # Only remove a failed build's stub index when we hold a REAL
        # cross-process lock — then no OTHER process could have built a good
        # index concurrently, so any .codegraph present is our own stub. Under
        # the "nolock" fallback we can't prove that, so we must NOT rmtree
        # (could delete a concurrent process's fresh good index).
        _have_lock = not isinstance(_fl, str)
        try:
            if indexed(cwd):            # built by the other process meanwhile
                return True
            try:
                # NOTE: `init` takes a POSITIONAL path (`codegraph init <path>`)
                # — the query subcommands use `-p/--path`. Passing `--path` here
                # made the binary reject it ("unknown option '--path'") so
                # autobuild silently failed. split() so an override like
                # "init --force" becomes two argv tokens, not one bogus token.
                p = subprocess.run([exe, *_init_cmd().split(), repo],
                                   capture_output=True, text=True,
                                   timeout=timeout_s or _build_timeout_s())
            except Exception:  # noqa: BLE001 — timeout / spawn failure
                # A TIMEOUT means the PROCESS didn't exit in time — NOT that the
                # index is incomplete: a build that finished writing the DB but
                # overran on a slow teardown lands here with a VALID index. Mirror
                # the returncode path — only remove when the index is genuinely
                # absent; if a populated .codegraph exists, keep + use it.
                if indexed(cwd):
                    _FAILED.pop(repo, None)
                    return True
                if _have_lock:
                    _remove_partial_index(repo)
                _FAILED[repo] = _time.monotonic()
                return False
            # A non-zero exit (wrong subcommand, disk full, partial write) can
            # still leave a stub .codegraph — require BOTH a clean exit and a real
            # index; else remove the stub, negative-cache, stay off.
            if p.returncode != 0 or not indexed(cwd):
                if _have_lock:
                    _remove_partial_index(repo)
                _FAILED[repo] = _time.monotonic()
                return False
            _FAILED.pop(repo, None)
            return True
        finally:
            if hasattr(_fl, "close"):
                _fl.close()


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
