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
import sys
import threading
import time

log = logging.getLogger(__name__)

_LOCK = threading.Lock()
_PARKED: dict[str, tuple] = {}          # session → (ws, reason, parked_at)
_CONSENT: dict[str, set] = {}           # session → {folder realpath}
_SWEPT = {"done": False}
_EXCL: dict[str, dict] = {}             # repo → {"added": [...], "runs": n}

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

def exclude_acquire(repo: str, wt: str) -> None:
    """One more live run on ``repo``: the first adds the exclude lines (see
    team_workspace.ensure_exclude); later ones only count."""
    from aiforge_core.runtime.team_workspace import ensure_exclude
    with _LOCK:
        cur = _EXCL.get(repo)
        if cur is not None:
            cur["runs"] += 1
            return
        _EXCL[repo] = cur = {"added": [], "runs": 1}
    cur["added"] = ensure_exclude(wt)


def exclude_release(repo: str) -> None:
    """One run on ``repo`` ended: the last one takes the lines back out."""
    with _LOCK:
        cur = _EXCL.get(repo)
        if cur is None:
            return
        cur["runs"] -= 1
        if cur["runs"] > 0:
            return
        _EXCL.pop(repo, None)
    drop_exclude(repo, cur["added"])


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
        if has_pending(os.path.join(d, "work")):
            log.warning("team run %s holds uncommitted work — kept", d)
            continue
        _remove_run_dir(d)
        gone.append(d)
    return gone


def has_pending(wt: str, untracked: bool = True) -> bool:
    """True when the worktree ``wt`` has staged or changed files (and, with
    ``untracked``, new ones) that :func:`team_workspace.seal` would commit —
    the same pathspecs, so an ignored ``.vscode/`` or ``.env`` edit never
    counts: work that exists nowhere else."""
    from aiforge_core.runtime.team_workspace import _git, seal_pathspecs
    if not os.path.isdir(wt):
        return False
    try:
        p = _git(["status", "--porcelain",
                  "--untracked-files=" + ("all" if untracked else "no"),
                  "--", *seal_pathspecs()], wt)
    except Exception:  # noqa: BLE001
        return True
    return p.returncode == 0 and bool((p.stdout or "").strip())


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
    from aiforge_core.runtime.team_workspace import SealError, seal
    try:
        seal(ws.cwd, "aiforge: work so far (run paused)")
    except SealError as exc:        # the worktree is kept either way
        log.warning("parking %s: %s", ws.cwd, exc)
    ws.parked = True
    from aiforge_core.runtime import team_repo_net
    # no leftover alert / job pauses "continue"; the halt itself stays until
    # resume() or close, so a driver still in a model call cannot carry on
    team_repo_net.reset(ws, keep_halt=True)
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
    same = fold(repo or "") == fold(ws.repo)
    if fresh and same and not ws.closed and continues(prompt, reason):
        ws.parked = False
        from aiforge_core.runtime import team_repo_net
        team_repo_net.reset(ws)      # re-baseline: only NEW changes pause
        return ws, ""
    return None, _close(ws)


# "actually, build X instead" / "new task: …" — a new request, not an answer.
_NEW_REQUEST_RE = re.compile(
    r"\b(?:instead|new\s+(?:task|request|feature)|forget\s+(?:it|that|this)|"
    r"never\s*mind|scratch\s+that|something\s+else|different\s+thing)\b|"
    r"^\s*(?:actually|now|next|also)\b[\s,]+(?:please\s+)?(?:build|create|"
    r"implement|add|make|write|refactor|fix|change|rewrite)\b|"
    r"^\s*(?:please\s+)?(?:build|create|implement|make|write|refactor|"
    r"rewrite|design|set\s+up|scaffold)\b", re.I)
_YES_RE = re.compile(r"^\s*(?:y(?:es|eah|ep|up)?|ok(?:ay)?|sure|do\s+it|"
                     r"sounds\s+good|looks\s+good|lgtm|approved?|right|"
                     r"correct|please\s+do)\b", re.I)


# A reply that asks for work of its own is a new request, not an answer:
# it opens with an action verb or a request ("can you …", "please add …").
_ACTION_VERBS = (r"fix|add|build|create|implement|make|write|refactor|change|"
                 r"rewrite|remove|delete|update|design|set\s+up|scaffold|run|"
                 r"deploy|test|debug|migrate|rename|move|install|port|convert|"
                 r"generate|improve|optimi[sz]e|clean\s+up|split|merge")
_REQUEST_RE = re.compile(
    r"^\s*(?:(?:ok(?:ay)?|now|next|also|then|and|so)[\s,]+)*(?:please\s+)?(?:"
    + _ACTION_VERBS + r")\b|\b(?:can|could|would|will)\s+you\b|"
    r"\bplease\s+(?:" + _ACTION_VERBS + r")\b|\bI\s+(?:want|need|would\s+"
    r"like|'d\s+like)\s+(?:you\s+to|to)\b", re.I)


def continues(prompt: str, reason: str) -> bool:
    """Does ``prompt`` carry on a run parked for ``reason``? "continue" /
    "go on" / "yes" always; for a pending question also a plausible ANSWER —
    a short reply with no request of its own ("euros, two decimals", "use
    postgres"). "fix the login bug", "can you add dark mode?" and "yes, but
    build X instead" are new requests: the parked run is closed."""
    p = str(prompt or "").strip()
    if _CONTINUE_RE.match(p):
        return True
    if not p or _NEW_REQUEST_RE.search(p):
        return False
    if _YES_RE.match(p) and not _REQUEST_RE.search(_YES_RE.sub("", p, 1)):
        return True
    return (reason == "question" and len(p.split()) <= 40
            and not _REQUEST_RE.search(p))


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
    repo = fold(ws.repo)

    def _overlaps(r: str) -> bool:
        real = fold(r) or os.sep
        return (real == repo or real.startswith(repo + os.sep)
                or repo.startswith(real.rstrip(os.sep) + os.sep))
    return [r for r in roots if not _overlaps(r)]


def fold(path: str) -> str:
    """``path`` resolved for comparison: realpath, no trailing ``/`` and, on
    macOS (case-insensitive by default), lower-cased — ``/Users/Me/Proj``
    and ``/users/me/proj`` are the same folder there."""
    try:
        p = os.path.realpath(os.path.expanduser(str(path)))
    except Exception:  # noqa: BLE001
        p = str(path)
    p = os.path.normcase(p).rstrip(os.sep) or os.sep
    return p.lower() if sys.platform == "darwin" else p


def _under(path: str, root_folded: str) -> bool:
    fp = fold(path)
    return fp == root_folded or fp.startswith(root_folded.rstrip(os.sep) + os.sep)


def repo_writes(cwd, name: str, args: dict) -> list[str]:
    """The paths under the user's REAL repo that a shell command in a team
    run enters or names in a command that does not only read them
    (runtime/team_repo_guard). Reads and copies FROM the repo pass. Empty
    outside a team run."""
    from aiforge_core.runtime import shell_writes as sw
    from aiforge_core.runtime.team_workspace import for_cwd
    ws = for_cwd(cwd) if cwd else None
    if ws is None or name not in sw.SHELL_TOOLS:
        return []
    cmd = sw.command_of(args)
    if not cmd:
        return []
    from aiforge_core.runtime.team_repo_guard import touches
    try:
        return touches(cmd, os.path.realpath(ws.cwd), fold(ws.repo))
    except Exception as exc:  # noqa: BLE001 — a matcher bug never blocks
        log.debug("repo write check skipped: %s", exc)
        return []


__all__ = ["consented", "continues", "drop_exclude", "drop_if_empty",
           "exclude_acquire", "exclude_release", "fold", "forget_session",
           "has_pending", "jail_roots", "park",
           "parked", "remember_consent", "repo_writes", "resume",
           "sweep_once", "sweep_stale"]
