"""Building the codegraph index on demand: per-repo locks, cooldowns, trusting
an existing index, and ensure_indexed."""
from __future__ import annotations

import os
import subprocess
import threading as _threading  # noqa: E402
import time as _time  # noqa: E402
from collections import defaultdict as _defaultdict  # noqa: E402


def _pkg():
    """``codegraph``, the module this code was split from, looked up on each call.

    Only names read through here follow a patch on ``codegraph``; patch any other
    name on this module."""
    import aiforge_core.runtime.tools.codegraph as package
    return package


_CODEGRAPH = '.codegraph'
_MISSING_SYMBOL = "missing 'symbol'"

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


def _trusted_existing(_cwd, repo_canon: str) -> bool | None:
    """Whether an EXISTING index can be trusted without building.

    Integrity-verifies ONCE per repo (cached) so a corrupt index left by a
    CRASHED prior process (OOM / SIGKILL mid-init — no in-process cleanup) isn't
    trusted forever. True = trust it; None = it read corrupt, so fall through to
    the locked build path.
    """
    if repo_canon in _VERIFIED_HEALTHY:
        return True
    if not _pkg()._db_corrupt(repo_canon):
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
    pkg = _pkg()
    return bool(pkg.indexed(cwd)
                and (repo in _VERIFIED_HEALTHY or not pkg._db_corrupt(repo)))


def _mark_failed(repo: str, have_lock: bool) -> bool:
    """Drop a stub index and start the cooldown. The stub is removed when we
    hold the REAL cross-process lock (so no other process could have built a
    good index concurrently) OR when it is PROVEN corrupt — a corrupt index is
    never a concurrent process's good one. Under the "nolock" fallback an
    unprobeable index is left alone."""
    pkg = _pkg()
    if have_lock or pkg._db_corrupt(repo):
        pkg._remove_partial_index(repo)
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
    pkg = _pkg()
    if pkg.indexed(cwd) and not pkg._db_corrupt(repo):
        _FAILED.pop(repo, None)
        if pkg._index_healthy(repo):            # proven-good → trust fast path
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
    pkg = _pkg()
    if _already_good(cwd, repo):
        return True
    # Re-check the negative cache INSIDE the lock — a thread that queued while
    # another built-and-failed must honor that fresh failure, not re-run the
    # full blocking build.
    if _in_cooldown(repo):
        return False
    exe = pkg._bin()
    if not exe:
        return False
    # Cross-PROCESS guard: another process may already be building this same
    # repo's index (concurrent tickets). None = contended → skip the duplicate
    # build (the other process will finish; next turn re-checks).
    fl = pkg._acquire_build_lock(repo)
    if fl is None:
        return False
    have_lock = not isinstance(fl, str)
    try:
        if _already_good(cwd, repo):    # built by the other process meanwhile
            return True
        # Corrupt leftover AND we hold the real cross-process lock → no other
        # process is building, so it's OUR crashed stub: remove it so `init`
        # starts clean. Under the nolock fallback we can't prove that.
        if pkg.indexed(cwd) and have_lock:
            pkg._remove_partial_index(repo)
        return _init_and_verify(cwd, repo, exe, timeout_s, have_lock)
    finally:
        if hasattr(fl, "close"):
            fl.close()


def _init_and_verify(cwd, repo: str, exe: str, timeout_s, have_lock: bool) -> bool:
    pkg = _pkg()
    try:
        p = pkg._run_init(exe, repo, timeout_s)
    except Exception:  # noqa: BLE001 — timeout / spawn failure
        return _after_timeout(cwd, repo, have_lock)
    # A non-zero exit (wrong subcommand, disk full, partial write) can still
    # leave a stub .codegraph. A clean exit (rc 0) is TRUSTED — we do NOT
    # integrity-gate it (the binary may name its DB with an extension we can't
    # probe; gating deleted every good build).
    if p.returncode != 0 or not pkg.indexed(cwd):
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
    pkg = _pkg()
    if not pkg._autobuild_enabled() or pkg._disabled() or not pkg.available():
        return pkg.indexed(cwd)
    if pkg.indexed(cwd):
        trusted = _trusted_existing(cwd, _canon_repo(pkg._repo(cwd)))
        if trusted:
            return True
    repo = pkg._repo(cwd)
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
