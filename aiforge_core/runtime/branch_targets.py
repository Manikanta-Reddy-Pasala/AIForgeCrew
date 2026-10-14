"""A folder per (repository, branch) a chat works on.

A task that changes two branches of one repository (a fix on ``main`` and
the same fix on ``release/2``), or branches in several repositories, used to
switch the one checkout back and forth: edits for one branch were made while
the other was checked out, and commits landed on the wrong branch.

Now every branch the chat works on is a folder of its own: the checkout that
already has the branch, or a git worktree made for it next to the chat's own
(``<repo>/.aiforge-worktrees/branch-<name>/work``, kept out of ``git status``
by ``.git/info/exclude``). An edit for a branch is made in its folder, and a
commit there can only land on that branch. Nothing is switched.

:func:`ensure` finds or makes the folder; :func:`remove` takes away one it
made, when the chat is deleted and the folder holds no uncommitted change (the
branch and its commits are kept).
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import threading

log = logging.getLogger(__name__)

_LOCKS: dict = {}
_LOCKS_GUARD = threading.Lock()


def _lock(repo: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(os.path.realpath(repo), threading.Lock())


def _git(args, cwd: str, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          timeout=timeout)


def _out(args, cwd: str) -> str:
    try:
        p = _git(args, cwd, 30)
    except Exception:  # noqa: BLE001
        return ""
    return (p.stdout or "").strip() if p.returncode == 0 else ""


def checked_out(repo: str) -> dict:
    """``{branch: folder}`` for every worktree of ``repo`` that has a branch."""
    out: dict = {}
    path, branch = "", ""

    def keep():
        # A worktree git lists whose folder is gone ("prunable") is no folder.
        if path and branch and os.path.isdir(path):
            out[branch] = os.path.realpath(path)

    for line in _out(["worktree", "list", "--porcelain"], repo).splitlines() + [""]:
        if line.startswith("worktree "):
            keep()
            path, branch = line[len("worktree "):].strip(), ""
        elif line.startswith("branch refs/heads/"):
            branch = line[len("branch refs/heads/"):].strip()
        elif not line.strip():
            keep()
            path, branch = "", ""
    return out


def exists(repo: str, branch: str) -> bool:
    return bool(_out(["rev-parse", "--verify", "-q", f"refs/heads/{branch}"], repo))


def _remote_of(repo: str, branch: str) -> str:
    """``origin/<branch>`` (or another remote's) when only the remote has it."""
    refs = _out(["for-each-ref", "--format=%(refname:short)", "refs/remotes"],
                repo).splitlines()
    exact = [r for r in refs if "/" in r and r.split("/", 1)[1] == branch]
    if f"origin/{branch}" in exact:
        return f"origin/{branch}"
    return exact[0] if len(exact) == 1 else ""


def _slug(branch: str) -> str:
    """A folder name for ``branch``; the hash keeps ``feat/a`` and ``feat-a``
    (and names differing only in case) apart."""
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip("-") or "branch"
    tag = hashlib.sha1(branch.encode("utf-8"), usedforsecurity=False).hexdigest()[:6]
    return f"{base[:60]}-{tag}"


def ensure(repo: str, branch: str, base: str = "") -> dict:
    """The folder where ``repo`` has ``branch`` checked out: ``{"path",
    "made", "new_branch"}``. Makes a worktree when no folder has it; makes the
    branch (from ``base``, a remote branch of that name, or HEAD) when the
    repository does not have it. Raises ``ValueError`` with a readable reason."""
    if not branch or branch.startswith("-") or ".." in branch:
        raise ValueError(f"not a branch name: {branch!r}")
    if _git(["check-ref-format", "--branch", branch], repo, 15).returncode != 0:
        raise ValueError(f"not a valid branch name: {branch!r}")
    with _lock(repo):
        return _ensure(repo, branch, base)


def _ensure(repo: str, branch: str, base: str) -> dict:
    have = checked_out(repo)
    if branch in have:
        return {"path": have[branch], "made": False, "new_branch": False}
    from aiforge_core.runtime import chat_worktree as cw
    from aiforge_core.runtime import team_workspace as tw
    wt = os.path.join(cw._run_dir(repo, f"branch-{_slug(branch)}"), "work")
    if os.path.isdir(wt):
        raise ValueError(f"{wt} exists but is not a checkout of {branch}")
    tw.ensure_exclude(repo)
    os.makedirs(os.path.dirname(wt), exist_ok=True)
    new = False
    if exists(repo, branch):
        args = ["worktree", "add", "-q", wt, branch]
    elif _remote_of(repo, branch):
        args = ["worktree", "add", "-q", "--track", "-b", branch, wt, _remote_of(repo, branch)]
        new = True
    else:
        start = base or "HEAD"
        if not _out(["rev-parse", "--verify", "-q", start + "^{commit}"], repo):
            raise ValueError(f"cannot start branch {branch} from {start!r}: not found")
        args = ["worktree", "add", "-q", "-b", branch, wt, start]
        new = True
    try:
        p = _git(args, repo, 900)
    except subprocess.TimeoutExpired as exc:
        _cleanup(repo, wt)
        raise ValueError("making the worktree took too long") from exc
    if p.returncode != 0:
        _cleanup(repo, wt)
        raise ValueError("git could not make a worktree for " + branch + ": "
                         + (p.stderr or p.stdout or "").strip()[:200])
    return {"path": os.path.realpath(wt), "made": True, "new_branch": new}


def _cleanup(repo: str, wt: str) -> None:
    """A half-made worktree out of the way, so the next try can make it."""
    shutil.rmtree(os.path.dirname(wt), ignore_errors=True)
    try:
        _git(["worktree", "prune"], repo, 30)
    except Exception:  # noqa: BLE001
        pass


def remove(repo: str, path: str) -> bool:
    """Take away a worktree :func:`ensure` made, unless it holds uncommitted
    work. The branch stays."""
    if not path or not os.path.isdir(path):
        return False
    if _out(["status", "--porcelain"], path):
        return False                      # never drop work that is not committed
    try:
        ok = _git(["worktree", "remove", path], repo, 120).returncode == 0
        _git(["worktree", "prune"], repo, 30)
    except Exception:  # noqa: BLE001
        return False
    if ok:
        parent = os.path.dirname(path)
        try:
            os.rmdir(parent)              # the run dir, when nothing else is in it
        except OSError:
            pass
    return ok


__all__ = ["checked_out", "ensure", "exists", "remove"]
