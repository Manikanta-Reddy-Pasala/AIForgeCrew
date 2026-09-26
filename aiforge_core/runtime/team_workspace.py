"""A team run in the user's OWN repo works on a new branch, in its own worktree.

The team pipeline merges subtask branches, commits a baseline and lets a repair
engine patch files in place. Pointed straight at a folder the user named, it
merged onto the branch they had checked out, edited their ``.gitignore``, wrote
``SPEC.md`` / ``.aiforge-baseline`` into their tree and left the repair
engine's edits uncommitted there.

:func:`open_run` instead creates branch ``aiforge/<slug>-<token>`` from the
user's HEAD and checks it out in a separate worktree under the config dir. The
pipeline's cwd is that worktree: every commit, merge and repair lands on the
new branch, and the user's working tree and current branch are never touched.
:func:`seal` commits whatever a writer left in the run worktree (repairs, a
sequential team's edits) onto the branch; :func:`close` removes the worktree,
keeps the branch, and fast-forwards the user's branch to it only when the user
asked for that (or the folder was not a repo until they let us initialise it).

``SPEC.md`` for such a run lives next to the worktree, not in the repo
(:func:`spec_path`).
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import uuid
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

_LOCK = threading.Lock()
_RUNS: dict[str, TeamWorkspace] = {}

# What the pipeline leaves in a run worktree that must never be committed —
# kept out through .git/info/exclude, never through the user's .gitignore.
_EXCLUDE_LINES = (".aiforge/", ".aiforge-worktrees/", ".aiforge-workspace",
                  ".aiforge-venv/", ".aiforge-contracts/", ".aiforge-baseline",
                  "__pycache__/", ".pytest_cache/")
_OWN_ARTIFACTS = ("SPEC.md", ".aiforge-baseline", ".aiforge-workspace",
                  ".aiforge-contracts", ".aiforge-worktrees", ".aiforge")
# "…and commit it on my branch" / "merge it into main" — the user asked for the
# result on the branch they have checked out, not a side branch.
_APPLY_RE = re.compile(
    r"\b(on|to|into|onto)\s+(my|the\s+current|this|the\s+checked[- ]out)\s+"
    r"branch\b|\bcommit\s+(it\s+|them\s+|this\s+)?(directly\s+)?(to|on|into)\s+"
    r"(main|master|develop|my\s+branch)\b|\bmerge\s+(it|them|the\s+result)\s+"
    r"(in|into)\s+(main|master|my\s+branch|the\s+current\s+branch)\b", re.I)
_MAX_INIT_FILES = 2000


@dataclass
class TeamWorkspace:
    repo: str
    cwd: str
    branch: str
    user_branch: str
    start_sha: str
    run_dir: str
    dirty: list[str] = field(default_factory=list)
    apply: bool = False
    fresh_repo: bool = False
    announced: bool = False
    closed: bool = False
    session_cwd: str = ""

    def summary(self) -> str:
        where = f"`{self.user_branch}`" if self.user_branch else "your HEAD"
        if self.apply:
            return (f"The team worked on branch `{self.branch}` in a separate "
                    f"worktree; it is fast-forwarded onto {where} as you asked.")
        return (f"Your checked-out branch {where} and working tree in "
                f"`{self.repo}` were not touched: the team's commits are on the "
                f"new branch `{self.branch}`. Review with `git diff "
                f"{self.user_branch or 'HEAD'}...{self.branch}` and merge it "
                f"with `git merge {self.branch}` when you are happy.")


def _git(args, cwd, timeout=60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, timeout=timeout)


def _out(args, cwd) -> str:
    try:
        p = _git(args, cwd)
    except Exception:  # noqa: BLE001
        return ""
    return (p.stdout or "").strip() if p.returncode == 0 else ""


def _key(path: str) -> str:
    try:
        return os.path.realpath(path)
    except Exception:  # noqa: BLE001
        return str(path)


def for_cwd(cwd) -> TeamWorkspace | None:
    """The run registered for ``cwd`` (the run worktree or anything inside
    it, e.g. a subtask worktree), else None."""
    if not cwd:
        return None
    k = _key(cwd)
    with _LOCK:
        for root, ws in _RUNS.items():
            if k == root or k.startswith(root.rstrip(os.sep) + os.sep):
                return ws
    return None


def spec_path(cwd: str) -> str:
    """Where SPEC.md lives for a run in ``cwd``: beside the run worktree for a
    user-repo run; ``cwd/SPEC.md`` in an AIForge workspace; for any other
    folder inside a git repo, the repo's git dir — never a file of the user's
    project."""
    ws = for_cwd(cwd)
    if ws is not None:
        return os.path.join(ws.run_dir, "SPEC.md")
    try:
        from aiforge_core.runtime.parallel_subtasks._planning import _is_managed_workspace
        managed = _is_managed_workspace(cwd)
    except Exception:  # noqa: BLE001
        managed = False
    here = os.path.join(cwd, "SPEC.md")
    if not managed and os.path.isdir(cwd):
        gd = _out(["rev-parse", "--absolute-git-dir"], cwd)
        # An untracked SPEC.md already there is a pipeline file from an
        # earlier run; a tracked one, or none at all, stays the user's.
        if gd and not (os.path.isfile(here) and not _out(
                ["ls-files", "--", "SPEC.md"], cwd)):
            return os.path.join(gd, "aiforge", "SPEC.md")
    return here


def write_spec(cwd: str, text: str) -> str:
    """Write SPEC.md to :func:`spec_path`; returns the path written."""
    p = spec_path(cwd)
    if _key(os.path.dirname(p)) != _key(cwd):
        os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(text)
    ws = for_cwd(cwd)
    if ws is not None and ws.session_cwd:
        # The chat's own workspace shows SPEC.md in the subtask dock.
        try:
            with open(os.path.join(ws.session_cwd, "SPEC.md"), "w",
                      encoding="utf-8") as fh:
                fh.write(text)
        except OSError:
            pass
    return p


def ensure_exclude(repo: str) -> None:
    """Keep the pipeline's own artifacts out of ``git status`` / ``git add``
    through ``.git/info/exclude`` — the user's ``.gitignore`` is theirs."""
    path = _out(["rev-parse", "--git-path", "info/exclude"], repo)
    if not path:
        return
    path = path if os.path.isabs(path) else os.path.join(repo, path)
    try:
        have = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
        missing = [ln for ln in _EXCLUDE_LINES if ln not in have.splitlines()]
        if missing:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(("" if not have or have.endswith("\n") else "\n")
                         + "# AIForge team-run artifacts\n"
                         + "\n".join(missing) + "\n")
    except OSError as exc:
        log.debug("info/exclude update skipped: %s", exc)


def dirty_files(repo: str) -> list[str]:
    """Tracked files with uncommitted changes. Untracked files and the
    pipeline's own artifacts do not count: an untracked cache is not work a
    merge could lose."""
    try:
        out = _git(["status", "--porcelain", "--untracked-files=no"],
                   repo).stdout or ""
    except Exception:  # noqa: BLE001
        return []
    files = []
    for ln in out.splitlines():
        rel = ln[3:].strip().split(" -> ")[-1].strip('"')
        if rel and not any(rel == a or rel.startswith(a + "/")
                           for a in _OWN_ARTIFACTS):
            files.append(rel)
    return files


def wants_apply(texts) -> bool:
    return any(_APPLY_RE.search(str(t or "")) for t in (texts or ()))


def _slug(text: str) -> str:
    # Paths are where, not what: "In /Users/me/proj fix money.py" → fix-money-py.
    text = re.sub(r"(?<!\S)[~/]\S*", " ", str(text or ""))
    words = re.findall(r"[a-z0-9]+", text.lower())
    stop = {"in", "the", "a", "an", "so", "to", "and", "of", "there", "that",
            "users", "home", "every", "please", "do", "not"}
    keep = [w for w in words if w not in stop and not w.isdigit()][:4]
    return "-".join(keep)[:32] or "run"


def count_files(folder: str, limit: int = _MAX_INIT_FILES) -> int:
    n = 0
    for _root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".")
                   and d not in ("node_modules", "venv", "__pycache__")]
        n += len(files)
        if n > limit:
            break
    return n


def init_repo(folder: str) -> bool:
    """``git init`` + one baseline commit in a folder the user allowed.
    Refuses a folder with more than a few thousand files (not a project)."""
    if count_files(folder) > _MAX_INIT_FILES:
        return False
    if _git(["init", "-q"], folder).returncode != 0:
        return False
    ensure_exclude(folder)
    from aiforge_core.runtime.git_pr import _EXCLUDE_PATHSPECS
    _git(["add", "-A", "--", ".", *_EXCLUDE_PATHSPECS], folder)
    return _git(["-c", "user.email=aiforge@local", "-c", "user.name=aiforge",
                 "commit", "-q", "--allow-empty", "-m", "baseline before the "
                 "AIForge team run"], folder).returncode == 0


def _managed(path: str) -> str:
    """``path`` when it is an AIForge-owned workspace, else ''."""
    try:
        from aiforge_core.runtime.parallel_subtasks._planning import _is_managed_workspace
        return path if path and _is_managed_workspace(path) else ""
    except Exception:  # noqa: BLE001
        return ""


def _runs_root() -> str:
    from aiforge_core.config.paths import config_dir
    return os.path.join(str(config_dir()), "team-runs")


def open_run(repo: str, prompt: str, *, apply: bool = False,
             fresh_repo: bool = False, session_cwd: str = "") -> TeamWorkspace:
    """Create the run branch + worktree for ``repo`` and register it."""
    repo = _key(repo)
    token = uuid.uuid4().hex[:6]
    branch = f"aiforge/{_slug(prompt)}-{token}"
    run_dir = os.path.join(_runs_root(), f"{os.path.basename(repo)}-{token}")
    wt = os.path.join(run_dir, "work")
    os.makedirs(run_dir, exist_ok=True)
    start = _out(["rev-parse", "HEAD"], repo)
    if not start:
        raise RuntimeError(f"{repo} has no commit to branch from")
    p = _git(["worktree", "add", "-q", "-b", branch, wt, start], repo, 120)
    if p.returncode != 0:
        raise RuntimeError(f"could not create a worktree for {repo}: "
                           f"{(p.stderr or '').strip()[:200]}")
    ensure_exclude(wt)
    if not _out(["var", "GIT_COMMITTER_IDENT"], wt):
        # No git identity on this host: commit as aiforge through the
        # environment, never by writing into the user's repo config.
        for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
            os.environ.setdefault(k, "aiforge")
        for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
            os.environ.setdefault(k, "aiforge@local")
    ws = TeamWorkspace(repo=repo, cwd=_key(wt), branch=branch,
                       user_branch=_out(["symbolic-ref", "--short", "-q",
                                         "HEAD"], repo),
                       start_sha=start, run_dir=run_dir,
                       dirty=dirty_files(repo), apply=apply,
                       fresh_repo=fresh_repo, session_cwd=_managed(session_cwd))
    with _LOCK:
        _RUNS[ws.cwd] = ws
    return ws


def seal(cwd: str, message: str = "aiforge: remaining edits") -> list[str]:
    """Commit every change a writer left uncommitted in ``cwd`` (the run
    worktree or an AIForge workspace) after putting read-only paths back.
    Returns the files committed. Never used on a user's own checkout."""
    from aiforge_core.runtime.git_pr import _EXCLUDE_PATHSPECS
    from aiforge_core.runtime.parallel_subtasks import _protected
    try:
        _protected.revert(cwd, "HEAD")
        _git(["add", "-A", "--", ".", *_EXCLUDE_PATHSPECS,
              *(f":(exclude){a}" for a in _OWN_ARTIFACTS)], cwd)
        names = _out(["diff", "--cached", "--name-only"], cwd).splitlines()
        if names:
            _git(["commit", "-q", "-m", message], cwd)
        return [n for n in names if n]
    except Exception as exc:  # noqa: BLE001
        log.debug("seal skipped: %s", exc)
        return []


def close(ws: TeamWorkspace):
    """Commit leftovers, drop the worktree (the branch stays) and, when asked,
    fast-forward the user's branch. Yields at most one note for the chat."""
    if ws is None or ws.closed:
        return
    ws.closed = True
    from aiforge_core.runtime.parallel_subtasks import _protected
    back = _protected.revert(ws.cwd, "HEAD")
    left = seal(ws.cwd, "aiforge: edits left by the team run")
    _protected.clear(ws.cwd)
    _git(["worktree", "remove", "--force", ws.cwd], ws.repo, 120)
    _git(["worktree", "prune"], ws.repo)
    import shutil
    shutil.rmtree(ws.run_dir, ignore_errors=True)   # SPEC.md was mirrored
    with _LOCK:
        _RUNS.pop(ws.cwd, None)
    notes = []
    if back:
        notes.append("Put back read-only file(s) the run changed: "
                     + ", ".join(f"`{b}`" for b in back[:6]) + ".")
    if left:
        notes.append(f"Committed {len(left)} file(s) the run left uncommitted "
                     f"onto `{ws.branch}`.")
    if ws.apply or ws.fresh_repo:
        notes.append(_fast_forward(ws))
    elif not ws.announced:
        notes.append(ws.summary())
    text = " ".join(n for n in notes if n)
    if text:
        yield {"type": "message", "role": "system", "supplementary": True,
               "text": text}


def _fast_forward(ws: TeamWorkspace) -> str:
    cur = _out(["rev-parse", "HEAD"], ws.repo)
    if cur != ws.start_sha:
        return (f"Your branch moved during the run, so the result stays on "
                f"`{ws.branch}` — merge it yourself.")
    if dirty_files(ws.repo):
        return (f"Your working tree has uncommitted changes, so the result "
                f"stays on `{ws.branch}` — merge it when they are committed.")
    p = _git(["merge", "--ff-only", "-q", ws.branch], ws.repo, 120)
    if p.returncode != 0:
        return (f"Could not fast-forward onto your branch; the result is on "
                f"`{ws.branch}`.")
    return (f"Applied the team's commits onto "
            f"`{ws.user_branch or 'HEAD'}` (fast-forward from `{ws.branch}`).")


__all__ = ["TeamWorkspace", "close", "dirty_files", "ensure_exclude",
           "for_cwd", "init_repo", "open_run", "seal", "spec_path",
           "wants_apply", "write_spec"]
