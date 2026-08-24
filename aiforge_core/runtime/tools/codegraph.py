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


def _db_files(repo: str) -> list:
    """The SQLite DB file(s) inside a built ``.codegraph``, EXCLUDING WAL/SHM/
    journal sidecars (not standalone DBs). Empty when the binary names its store
    with an extension we don't recognise — callers must treat 'no DB found' as
    'can't verify', NOT 'corrupt'."""
    import glob
    d = os.path.join(repo, ".codegraph")
    try:
        dbs = (glob.glob(os.path.join(d, "**", "*.db"), recursive=True)
               + glob.glob(os.path.join(d, "**", "*.sqlite"), recursive=True))
    except Exception:  # noqa: BLE001
        return []
    return [x for x in dbs
            if not x.endswith(("-wal", "-shm", "-journal", ".db-wal", ".db-shm",
                               ".sqlite-wal", ".sqlite-shm"))]


def _db_corrupt(repo: str) -> bool:
    """True ONLY when a recognisable DB file EXISTS and FAILS a SQLite
    quick_check — i.e. PROVEN corrupt. 'No DB found' (unknown filename) → False
    (not proven corrupt), so a valid build whose store we can't locate is never
    wrongly deleted. Never raises."""
    import sqlite3
    for db in _db_files(repo):
        try:
            # immutable=1: read-only without needing a -shm/-wal (build is done,
            # no writer), so a WAL-mode DB opens cleanly.
            con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True,
                                  timeout=2)
            row = con.execute("PRAGMA quick_check").fetchone()
            con.close()
            if not row or str(row[0]).lower() != "ok":
                return True
        except Exception:  # noqa: BLE001 — corrupt / unreadable
            return True
    return False


def _index_healthy(repo: str) -> bool:
    """A USABLE index — a recognisable DB is present AND passes quick_check. Used
    for the timeout-keep decision (a slow-teardown overrun with a complete DB is
    kept; a mid-write timeout with a corrupt/absent DB is not). Distinct from
    ``not _db_corrupt`` which is True even when NO db is found."""
    return bool(_db_files(repo)) and not _db_corrupt(repo)


def _remove_partial_index(repo: str) -> None:
    """Best-effort remove a half-written ``.codegraph`` left by a failed/timed-out
    build. Safe: ensure_indexed only builds when the repo was NOT already
    indexed, so any index present after a failed build is a stub from that
    attempt, not a good prior index. Never raises."""
    _VERIFIED_HEALTHY.discard(repo)
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
# Repos whose index passed a quick_check this process — skip re-verifying every
# turn (the fast-path integrity check is one-shot per repo).
_VERIFIED_HEALTHY: "set[str]" = set()


def _canon_repo(repo: str) -> str:
    """Canonical (realpath + normcase) repo key — so the thread lock, negative
    cache, flock and health cache all key on ONE path regardless of spelling."""
    try:
        return os.path.normcase(os.path.realpath(repo))
    except Exception:  # noqa: BLE001
        return repo
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


def _trusted_existing(cwd, repo_canon: str) -> bool | None:
    """Whether an EXISTING index can be trusted without building.

    Integrity-verifies ONCE per repo (cached) so a corrupt index left by a
    CRASHED prior process (OOM / SIGKILL mid-init — no in-process cleanup) isn't
    trusted forever. True = trust it; None = it read corrupt, so fall through to
    the locked build path.
    """
    if repo_canon in _VERIFIED_HEALTHY:
        return True
    if not _db_corrupt(repo_canon):
        _VERIFIED_HEALTHY.add(repo_canon)
        return True
    # PROVEN-corrupt crash-leftover. Do NOT delete it HERE — this fast path
    # holds no lock, and an index that reads corrupt right now can be a
    # CONCURRENT process's DB caught mid-write (torn header/pages). rmtree'ing
    # it would destroy a healthy build another process is finishing. The locked
    # path removes + rebuilds under the cross-process flock. Clear the negative
    # cache so the rebuild isn't cooldown-blocked.
    _FAILED.pop(repo_canon, None)
    return None


def _in_cooldown(repo: str) -> bool:
    """A repo that failed/timed out must NOT re-trigger the (blocking) build
    every turn — that hung chat forever on a repo too big to index in the
    budget."""
    ts = _FAILED.get(repo)
    return ts is not None and (_time.monotonic() - ts) < _retry_cooldown_s()


def _already_good(cwd, repo: str) -> bool:
    """Built by another thread/process while we waited — trust it UNLESS it is
    the proven-corrupt leftover we fell through for."""
    return bool(indexed(cwd)
                and (repo in _VERIFIED_HEALTHY or not _db_corrupt(repo)))


def _mark_failed(repo: str, have_lock: bool) -> bool:
    """Drop a stub index and start the cooldown. The stub is removed when we
    hold the REAL cross-process lock (so no other process could have built a
    good index concurrently) OR when it is PROVEN corrupt — a corrupt index is
    never a concurrent process's good one. Under the "nolock" fallback an
    unprobeable index is left alone."""
    if have_lock or _db_corrupt(repo):
        _remove_partial_index(repo)
    _FAILED[repo] = _time.monotonic()
    return False


def _after_timeout(cwd, repo: str, have_lock: bool) -> bool:
    """A TIMEOUT means the PROCESS didn't exit in time — NOT necessarily that
    the index is incomplete: a build that finished writing the DB but overran on
    slow teardown is VALID. Keep the index UNLESS it is PROVEN corrupt. An
    unprobeable store (the binary named its DB with an extension we can't read)
    is trusted, exactly as the clean-exit path does — gating on _index_healthy
    here instead deleted a complete build whose DB we simply couldn't locate,
    and locked out rebuild for the cooldown."""
    if indexed(cwd) and not _db_corrupt(repo):
        _FAILED.pop(repo, None)
        if _index_healthy(repo):            # proven-good → trust fast path
            _VERIFIED_HEALTHY.add(repo)
        return True
    return _mark_failed(repo, have_lock)


def _run_init(exe: str, repo: str, timeout_s: int | None):
    """``codegraph init <path>`` — a POSITIONAL path; the query subcommands use
    ``-p/--path``. Passing ``--path`` here made the binary reject it ("unknown
    option '--path'") so autobuild silently failed. split() so an override like
    "init --force" becomes two argv tokens, not one bogus token."""
    return subprocess.run([exe, *_init_cmd().split(), repo],
                          capture_output=True, text=True,
                          timeout=timeout_s or _build_timeout_s())


def _build_locked(cwd, repo: str, timeout_s: int | None) -> bool:
    """The build itself, under the per-repo thread lock."""
    if _already_good(cwd, repo):
        return True
    # Re-check the negative cache INSIDE the lock — a thread that queued while
    # another built-and-failed must honor that fresh failure, not re-run the
    # full blocking build.
    if _in_cooldown(repo):
        return False
    exe = _bin()
    if not exe:
        return False
    # Cross-PROCESS guard: another process may already be building this same
    # repo's index (concurrent tickets). None = contended → skip the duplicate
    # build (the other process will finish; next turn re-checks).
    fl = _acquire_build_lock(repo)
    if fl is None:
        return False
    have_lock = not isinstance(fl, str)
    try:
        if _already_good(cwd, repo):    # built by the other process meanwhile
            return True
        # Corrupt leftover AND we hold the real cross-process lock → no other
        # process is building, so it's OUR crashed stub: remove it so `init`
        # starts clean. Under the nolock fallback we can't prove that.
        if indexed(cwd) and have_lock:
            _remove_partial_index(repo)
        return _init_and_verify(cwd, repo, exe, timeout_s, have_lock)
    finally:
        if hasattr(fl, "close"):
            fl.close()


def _init_and_verify(cwd, repo: str, exe: str, timeout_s, have_lock: bool) -> bool:
    try:
        p = _run_init(exe, repo, timeout_s)
    except Exception:  # noqa: BLE001 — timeout / spawn failure
        return _after_timeout(cwd, repo, have_lock)
    # A non-zero exit (wrong subcommand, disk full, partial write) can still
    # leave a stub .codegraph. A clean exit (rc 0) is TRUSTED — we do NOT
    # integrity-gate it (the binary may name its DB with an extension we can't
    # probe; gating deleted every good build).
    if p.returncode != 0 or not indexed(cwd):
        return _mark_failed(repo, have_lock)
    _FAILED.pop(repo, None)
    _VERIFIED_HEALTHY.add(repo)         # fresh clean build → trust fast path
    return True


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
        trusted = _trusted_existing(cwd, _canon_repo(_repo(cwd)))
        if trusted:
            return True
    repo = _repo(cwd)
    if not repo or not os.path.isdir(repo):
        return False
    # Canonicalize ONCE so the per-repo thread lock, the negative cache AND the
    # cross-process flock all key on the SAME path — else two spellings of one
    # repo (the "." fallback, a symlink) would get separate _FAILED / _LOCKS
    # entries and the flock's canonical guarantee wouldn't match them.
    repo = _canon_repo(repo)
    if _in_cooldown(repo):
        return False
    with _lock_for(repo):            # PER-REPO lock (not one global)
        return _build_locked(cwd, repo, timeout_s)


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
