"""The life of a team run's branch + worktree beyond one turn (see
team_workspace).

* :func:`drop_exclude` — the lines a run added to the repo's SHARED
  ``info/exclude`` come back out when the last run on that repo closes.
* :func:`drop_if_empty` — a run that committed nothing leaves no
  ``aiforge/*`` branch behind.
* :func:`sweep_stale` — worktrees under ``team-runs/`` that no live run owns
  (a crash, a killed server) are removed after some hours, once per process.
* :func:`park` / :func:`resume` — a run that stopped (Stop pressed) or asked
  the user a question keeps its branch and worktree, so "continue" or the
  answer carries on from its commits instead of a fresh branch from HEAD.
* :func:`remember_consent` / :func:`consented` — the user allowed a team run
  in a folder. This is NOT a write grant: the chat jail keeps refusing (or
  asking about) writes into the user's real checkout during the run
  (:func:`jail_roots`).
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import threading
import time

log = logging.getLogger(__name__)

_LOCK = threading.Lock()
_PARKED: dict[str, tuple] = {}          # session → (ws, reason, parked_at)
_CONSENT: dict[str, set] = {}           # session → {folder realpath}
_SWEPT = {"done": False}

_CONTINUE_RE = re.compile(
    r"^\s*(?:ok(?:ay)?[,.!]?\s+|yes[,.!]?\s+|please\s+)*(?:continue|resume|go\s+on|"
    r"keep\s+going|carry\s+on|proceed|go\s+ahead|pick\s+(?:it\s+)?up|finish"
    r"(?:\s+it)?|try\s+again|retry|restart\s+it)\b", re.I)


def _stale_hours() -> float:
    try:
        return float(os.environ.get("AIFORGE_TEAM_RUN_STALE_HOURS", "12"))
    except (TypeError, ValueError):
        return 12.0


# ── shared info/exclude ────────────────────────────────────────────────────

def drop_exclude(repo: str, added) -> None:
    """Remove the ``added`` lines (see team_workspace.ensure_exclude) from
    ``repo``'s info/exclude, leaving everything else as it was."""
    if not added or not repo:
        return
    from aiforge_core.runtime.team_workspace import exclude_path
    path = exclude_path(repo)
    if not path or not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        todo = list(added)
        kept = []
        for ln in lines:
            if ln in todo:
                todo.remove(ln)
                continue
            kept.append(ln)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(kept) + ("\n" if kept else ""))
    except OSError as exc:
        log.debug("info/exclude cleanup skipped: %s", exc)


# ── empty branches ─────────────────────────────────────────────────────────

def drop_if_empty(ws) -> bool:
    """Delete ``ws.branch`` when it has no commit beyond ``ws.start_sha``
    (the worktree must already be gone). True when deleted."""
    from aiforge_core.runtime.team_workspace import _git, _out
    if not ws.start_sha or ws.parked:
        return False
    n = _out(["rev-list", "--count", f"{ws.start_sha}..{ws.branch}"], ws.repo)
    if n != "0":
        return False
    return _git(["branch", "-D", ws.branch], ws.repo).returncode == 0


# ── stale worktrees ────────────────────────────────────────────────────────

def sweep_once() -> None:
    """:func:`sweep_stale` the first time a run opens in this process."""
    with _LOCK:
        if _SWEPT["done"]:
            return
        _SWEPT["done"] = True
    try:
        sweep_stale()
    except Exception as exc:  # noqa: BLE001 — never block a run over this
        log.debug("team-runs sweep skipped: %s", exc)


def sweep_stale(max_age_h: float | None = None) -> list[str]:
    """Remove ``team-runs/*`` folders older than ``max_age_h`` that no live
    or parked run owns: the worktree is unregistered from its repo (``git
    worktree prune``) and a branch with no commit of its own is deleted.
    Returns the folders removed."""
    from aiforge_core.runtime import team_workspace as tw
    root = tw._runs_root()
    if not os.path.isdir(root):
        return []
    age = (_stale_hours() if max_age_h is None else max_age_h) * 3600
    with tw._LOCK:
        live = {os.path.dirname(k) for k in tw._RUNS}
    with _LOCK:
        live |= {os.path.realpath(v[0].run_dir) for v in _PARKED.values()}
    gone = []
    now = time.time()
    for name in os.listdir(root):
        d = os.path.join(root, name)
        try:
            if os.path.realpath(d) in live or now - os.path.getmtime(d) < age:
                continue
        except OSError:
            continue
        _remove_run_dir(d)
        gone.append(d)
    return gone


def _remove_run_dir(d: str) -> None:
    from aiforge_core.runtime.team_workspace import _git, _out
    wt = os.path.join(d, "work")
    common = branch = ""
    try:
        with open(os.path.join(wt, ".git"), encoding="utf-8") as fh:
            gitdir = fh.read().split("gitdir:", 1)[-1].strip()
        common = os.path.dirname(os.path.dirname(gitdir))
        branch = _out(["symbolic-ref", "--short", "-q", "HEAD"], wt)
    except OSError:
        pass
    shutil.rmtree(d, ignore_errors=True)
    if not common or not os.path.isdir(common):
        return
    _git(["--git-dir", common, "worktree", "prune"], d if os.path.isdir(d)
         else os.path.dirname(d))
    if branch and branch.startswith("aiforge"):
        others = [r for r in _out(["--git-dir", common, "for-each-ref",
                                   "--format=%(refname)", "refs/heads"],
                                  os.path.dirname(d)).splitlines()
                  if r and r != f"refs/heads/{branch}"]
        own = _out(["--git-dir", common, "rev-list", "--count", branch,
                    "--not", *others], os.path.dirname(d)) if others else "1"
        if own == "0":
            _git(["--git-dir", common, "branch", "-D", branch],
                 os.path.dirname(d))


# ── resumable runs ─────────────────────────────────────────────────────────

def park(session_id, ws, reason: str) -> str:
    """Keep ``ws`` (commit what it left) for the next turn of the chat.
    Returns the note for the chat."""
    from aiforge_core.runtime.team_workspace import seal
    seal(ws.cwd, "aiforge: work so far (run paused)")
    ws.parked = True
    with _LOCK:
        old = _PARKED.pop(str(session_id), None)
        _PARKED[str(session_id)] = (ws, reason, time.time())
    if old is not None and old[0] is not ws:
        _close(old[0])
    return (f"The team's work so far stays on branch `{ws.branch}`; say "
            "\"continue\" (or answer the question) to carry on from it.")


def parked(session_id):
    with _LOCK:
        v = _PARKED.get(str(session_id))
    return v[0] if v else None


def resume(session_id, repo: str, prompt: str):
    """The parked run to continue for this turn, or None. A parked run that
    this turn does not continue (another repo, a new request, too old) is
    closed, and its note returned. ``(ws_or_None, note)``."""
    with _LOCK:
        v = _PARKED.pop(str(session_id), None)
    if v is None:
        return None, ""
    ws, reason, at = v
    fresh = time.time() - at < _stale_hours() * 3600
    same = os.path.realpath(repo or "") == ws.repo
    goes_on = reason == "question" or bool(_CONTINUE_RE.match(prompt or "")) \
        or (prompt or "").strip() == (ws.prompt or "").strip()
    if fresh and same and goes_on and not ws.closed:
        ws.parked = False
        return ws, ""
    return None, _close(ws)


def _close(ws) -> str:
    from aiforge_core.runtime.team_workspace import close_quiet
    ws.parked = False
    try:
        return close_quiet(ws)
    except Exception as exc:  # noqa: BLE001
        log.debug("closing a parked run failed: %s", exc)
        return ""


def forget_session(session_id) -> str:
    with _LOCK:
        v = _PARKED.pop(str(session_id), None)
    return _close(v[0]) if v else ""


# ── consent vs. write grants ───────────────────────────────────────────────

def remember_consent(session_id, folder: str) -> None:
    if session_id is None:
        return
    with _LOCK:
        _CONSENT.setdefault(str(session_id), set()).add(os.path.realpath(folder))


def consented(session_id, folder: str) -> bool:
    if session_id is None:
        return False
    with _LOCK:
        return os.path.realpath(folder) in _CONSENT.get(str(session_id), set())


def jail_roots(cwd, roots) -> list:
    """The chat jail's extra writable roots for an agent working in ``cwd``.
    Inside a team run's worktree, every root that is (or holds) the user's
    real repo is dropped — the run writes its worktree, and a write to the
    real checkout is refused or asked about. Other roots pass through."""
    from aiforge_core.runtime.team_workspace import for_cwd
    ws = for_cwd(cwd) if cwd else None
    roots = [r for r in (roots or ()) if r]
    if ws is None:
        return roots
    repo = ws.repo.rstrip(os.sep)

    def _overlaps(r: str) -> bool:
        real = os.path.realpath(r).rstrip(os.sep) or os.sep
        return (real == repo or real.startswith(repo + os.sep)
                or repo.startswith(real.rstrip(os.sep) + os.sep))
    return [r for r in roots if not _overlaps(r)]


__all__ = ["consented", "drop_exclude", "drop_if_empty", "forget_session",
           "jail_roots", "park", "parked", "remember_consent", "resume",
           "sweep_once", "sweep_stale"]
