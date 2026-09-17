"""Housekeeping for best-of-n attempts: cleanup, the disk preflight and the cancel check."""
from __future__ import annotations

import os

from aiforge_core.runtime.git_pr import _EXCLUDE_DIR_SEGMENTS


def _pkg():
    """``best_of_n``, the module this code was split from, looked up on each call.

    Only names read through here follow a patch on ``best_of_n``; patch any other
    name on this module."""
    import aiforge_core.runtime.best_of_n as package
    return package


def _cleanup(repo: str, attempt: dict) -> None:
    """Discard a (loser) attempt's worktree + branch — mirrors the best-effort
    cleanup ``parallel_subtasks.run_parallel`` does after merging."""
    pkg = _pkg()
    wt = attempt.get("worktree")
    if wt and os.path.isdir(wt):
        pkg._git(["worktree", "remove", "--force", wt], repo)
    if attempt.get("branch"):
        pkg._git(["branch", "-D", attempt["branch"]], repo)


def _tree_bytes(cwd: str, cap: int = 50_000) -> int:
    """Working-tree size (sum of file sizes), heavy artifact dirs pruned.

    Prunes node_modules, .venv, dist, build, .git, worktrees, caches… — the
    same set git_pr uses — so the estimate isn't inflated and the walk doesn't
    crawl into them. Bounded by ``cap`` files on huge trees.
    """
    total = scanned = 0
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in _EXCLUDE_DIR_SEGMENTS]
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
            scanned += 1
        if scanned > cap:
            break
    return total


def _disk_preflight(cwd: str, n: int, *, safety: float = 1.2) -> str | None:
    """B6/B7 — best-effort disk-space preflight before creating N worktrees.

    Compares ``n × tree × safety`` against the free bytes on the filesystem
    (``os.statvfs``). On a likely shortfall logs a clear warning with the
    numbers and returns it; NEVER blocks (the check itself soft-fails). No
    heavy deps — a bounded ``os.walk``."""
    pkg = _pkg()
    try:
        total = pkg._tree_bytes(cwd)
        if total <= 0:
            return None
        st = os.statvfs(cwd)
        free = st.f_bavail * st.f_frsize
        need = total * n * safety
        if free >= need:
            return None
        msg = (f"low disk: free≈{free} bytes < needed≈{int(need)} "
               f"(tree≈{total} × n={n} × {safety}); {n} worktrees may run "
               "out of space")
        pkg.log.warning("best_of_n %s", msg)
        return msg
    except Exception as exc:  # noqa: BLE001 — preflight must never block
        pkg.log.debug("best_of_n disk preflight skipped: %s", exc)
        return None


def _cancel_checker(session_id, cancel_event):
    """The RUN-SCOPED event is authoritative. The session token is a secondary
    trigger — when it fires we LATCH the event so cancellation sticks even after
    ``_gen``'s finally later pops the token (the race this closes): a detached
    worker reading a freshly-cleared token would otherwise see "not cancelled"
    and run all N + merge."""
    from aiforge_core.runtime import chat_cancel

    def _cancelled() -> bool:
        if cancel_event is not None and cancel_event.is_set():
            return True
        if session_id is not None and chat_cancel.is_cancelled(session_id):
            if cancel_event is not None:
                cancel_event.set()
            return True
        return False
    return _cancelled
