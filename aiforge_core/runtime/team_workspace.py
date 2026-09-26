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
# Only an explicit imperative in the CURRENT message puts the result on the
# user's checked-out branch: "commit it to my branch", "merge it into main",
# "apply it to my branch", "fast-forward". A description ("the test fails on
# my branch, fix it") is not a request, and an old message never counts.
# The verb must be framed as a request: at the start of a sentence / after a
# comma, after "and/then/please/also/just/now", or after "can you / could you
# / would you / I want you to". Never after "error:" or inside quotes.
_IMPERATIVE_AT = (r"(?:^|[.;!?\n]\s*|,\s*|\b(?:and|then|please|also|just|now)"
                  r"\s+|\b(?:can|could|would|will)\s+you\s+|\bI\s+(?:want|need|"
                  r"would\s+like|'d\s+like)\s+you\s+to\s+)(?:please\s+)?")
_OBJ = r"(?:(?:it|this|them|that|the\s+(?:result|changes?|fix|work|branch))\s+)?"
_DEST = (r"(?:main|master|develop|trunk|(?:my|the\s+current|this|the\s+checked"
         r"[- ]out|our)\s+(?:current\s+)?branch)\b")
_APPLY_RE = re.compile(
    _IMPERATIVE_AT + r"(?P<v>"
    r"(?:apply|commit|merge|push|land|put)\s+" + _OBJ
    + r"(?:directly\s+|straight\s+)?(?:to|on|onto|into|in)\s+" + _DEST
    + r"|fast[- ]forward\b(?!\s+(?:fails?|failed|failing|is|was|does|did|"
    r"doesn'?t|didn'?t|isn'?t|error|errors|broke|breaks)\b))", re.I | re.M)
# Quoted / pasted text is not the user's request.
_QUOTED = re.compile(r"```.*?```|`[^`\n]*`|\"[^\"\n]*\"|“[^”\n]*”", re.S)
_APPLY_NEG = re.compile(r"(?:\bdo\s+not|\bdon[’']?t|\bnever|\bnot|\bno)\s+"
                        r"(?:\w+\s+){0,2}$", re.I)
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
    # Commit identity for this run's git calls only (a host with none); never
    # written into os.environ or the user's repo config.
    ident: dict = field(default_factory=dict)
    # Kept alive across turns (Stop / a planner question) — see team_run_life.
    parked: bool = False
    prompt: str = ""

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
                          text=True, timeout=timeout, env=git_env(cwd))


_IDENT_KEYS = ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME", "GIT_AUTHOR_EMAIL",
               "GIT_COMMITTER_EMAIL")


def git_env(cwd) -> dict | None:
    """The environment for a git call in ``cwd``: the process env plus the
    run's commit identity when ``cwd`` belongs to a run that needs one; None
    (inherit) otherwise."""
    ws = for_cwd(cwd) if cwd else None
    if ws is None or not ws.ident:
        return None
    env = dict(os.environ)
    for k, v in ws.ident.items():
        env.setdefault(k, v)
    return env


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
    """Write SPEC.md to :func:`spec_path`; returns the path written. For a
    run in the user's repo, paths under the repo are written relative to it
    (the run's worktree), never as the user's real checkout."""
    p = spec_path(cwd)
    ws0 = for_cwd(cwd)
    if ws0 is not None:
        from aiforge_core.runtime.team_target import localize_paths
        text = localize_paths(text, ws0.repo)
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


_EXCLUDE_HEADER = "# AIForge team-run artifacts"


def exclude_path(repo: str) -> str:
    path = _out(["rev-parse", "--git-path", "info/exclude"], repo)
    if not path:
        return ""
    return path if os.path.isabs(path) else os.path.join(repo, path)


def ensure_exclude(repo: str) -> list[str]:
    """Keep the pipeline's own artifacts out of ``git status`` / ``git add``
    through ``.git/info/exclude`` — the user's ``.gitignore`` is theirs.
    Returns the lines it added (with the header), for
    :func:`team_run_life.drop_exclude` to take back out."""
    path = exclude_path(repo)
    if not path:
        return []
    try:
        have = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
        missing = [ln for ln in _EXCLUDE_LINES if ln not in have.splitlines()]
        if missing:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(("" if not have or have.endswith("\n") else "\n")
                         + _EXCLUDE_HEADER + "\n" + "\n".join(missing) + "\n")
            return [_EXCLUDE_HEADER, *missing]
    except OSError as exc:
        log.debug("info/exclude update skipped: %s", exc)
    return []


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
    """True when the CURRENT message (``texts[0]``, or a plain string) asks
    for the result on the user's branch — see :data:`_APPLY_RE`."""
    cur = texts if isinstance(texts, str) else next(iter(texts or ()), "")
    cur = str(cur or "").split("\n\n---\n[Interpreted request")[0]
    cur = _QUOTED.sub(" ", cur)
    return any(not _APPLY_NEG.search(cur[:m.start("v")])
               for m in _APPLY_RE.finditer(cur))


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
    added = ensure_exclude(folder)
    from aiforge_core.runtime.git_pr import _EXCLUDE_PATHSPECS
    from aiforge_core.runtime.team_run_life import drop_exclude
    _git(["add", "-A", "--", ".", *_EXCLUDE_PATHSPECS], folder)
    ok = _git(["-c", "user.email=aiforge@local", "-c", "user.name=aiforge",
               "commit", "-q", "--allow-empty", "-m", "baseline before the "
               "AIForge team run"], folder).returncode == 0
    drop_exclude(folder, added)          # the run adds (and removes) its own
    return ok


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
    from aiforge_core.runtime import team_run_life
    team_run_life.sweep_once()
    repo = _key(repo)
    token = uuid.uuid4().hex[:6]
    branch = f"{_branch_prefix(repo)}/{_slug(prompt)}-{token}"
    run_dir = os.path.join(_runs_root(), f"{os.path.basename(repo)}-{token}")
    wt = os.path.join(run_dir, "work")
    os.makedirs(run_dir, exist_ok=True)
    start = _out(["rev-parse", "HEAD"], repo)
    if not start:
        raise RuntimeError(f"{repo} has no commit to branch from")
    p = _git(["worktree", "add", "-q", "-b", branch, wt, start], repo, 120)
    if p.returncode != 0:
        import shutil
        shutil.rmtree(run_dir, ignore_errors=True)
        raise RuntimeError(f"could not create a worktree for {repo}: "
                           f"{(p.stderr or '').strip()[:200]}")
    ident = {}
    if not _out(["var", "GIT_COMMITTER_IDENT"], wt):
        # No git identity on this host: commit as aiforge through THIS run's
        # git calls — never os.environ, never the user's repo config.
        ident = {"GIT_AUTHOR_NAME": "aiforge", "GIT_COMMITTER_NAME": "aiforge",
                 "GIT_AUTHOR_EMAIL": "aiforge@local",
                 "GIT_COMMITTER_EMAIL": "aiforge@local"}
    ws = TeamWorkspace(repo=repo, cwd=_key(wt), branch=branch,
                       user_branch=_out(["symbolic-ref", "--short", "-q",
                                         "HEAD"], repo),
                       start_sha=start, run_dir=run_dir,
                       dirty=dirty_files(repo), apply=apply,
                       fresh_repo=fresh_repo, session_cwd=_managed(session_cwd),
                       ident=ident, prompt=str(prompt or ""))
    with _LOCK:
        _RUNS[ws.cwd] = ws
    team_run_life.exclude_acquire(ws.repo, wt)
    if os.path.isfile(os.path.join(wt, ".gitmodules")):
        # A fresh worktree has empty submodule folders: build and tests
        # would run against nothing.
        try:
            _git(["submodule", "update", "--init", "--recursive"], wt, 300)
        except Exception as exc:  # noqa: BLE001
            log.debug("submodule init skipped: %s", exc)
    return ws


def _branch_prefix(repo: str) -> str:
    """``aiforge`` — unless the repo has a branch named ``aiforge``, which
    makes every ``aiforge/…`` ref impossible; then ``aiforge-run``."""
    for name in ("aiforge", "aiforge-run"):
        if _git(["show-ref", "--verify", "-q", f"refs/heads/{name}"],
                repo).returncode != 0:
            return name
    return f"aiforge-run-{uuid.uuid4().hex[:4]}"


class SealError(RuntimeError):
    """``git commit`` refused the run's leftovers even without hooks and
    signing — the work is still (only) in the worktree."""


def seal(cwd: str, message: str = "aiforge: remaining edits") -> list[str]:
    """Commit every change a writer left uncommitted in ``cwd`` (the run
    worktree or an AIForge workspace) after putting read-only paths back.
    Returns the files committed. Never used on a user's own checkout.

    Raises :class:`SealError` when nothing could be committed: a caller that
    believed the files were committed would remove the worktree and lose
    them."""
    from aiforge_core.runtime.git_pr import _EXCLUDE_PATHSPECS
    from aiforge_core.runtime.parallel_subtasks import _protected
    ws = for_cwd(cwd)
    try:
        _protected.revert(cwd, ws.start_sha if ws is not None else "HEAD")
        _git(["add", "-A", "--", ".", *_EXCLUDE_PATHSPECS,
              *(f":(exclude){a}" for a in _OWN_ARTIFACTS)], cwd)
        names = _out(["diff", "--cached", "--name-only"], cwd).splitlines()
    except Exception as exc:  # noqa: BLE001
        log.debug("seal skipped: %s", exc)
        return []
    names = [n for n in names if n]
    if names and not _commit(cwd, message):
        raise SealError(f"could not commit {len(names)} file(s) in {cwd}")
    return names


def _commit(cwd: str, message: str) -> bool:
    """``git commit``; when a hook or commit signing refuses it, once more
    without them — the ``aiforge/*`` branch is the run's own."""
    p = _git(["commit", "-q", "-m", message], cwd)
    if p.returncode == 0:
        return True
    log.warning("commit in %s refused (%s); retrying without hooks/signing",
                cwd, (p.stderr or p.stdout or "").strip()[:200])
    p = _git(["-c", "commit.gpgsign=false", "commit", "-q", "--no-verify",
              "-m", message], cwd)
    return p.returncode == 0


def close(ws: TeamWorkspace):
    """Commit leftovers, drop the worktree (the branch stays — unless the run
    made no commit) and, when asked, fast-forward the user's branch. Yields
    at most one note for the chat."""
    text = close_quiet(ws)
    if text:
        yield {"type": "message", "role": "system", "supplementary": True,
               "text": text}


def close_quiet(ws: TeamWorkspace) -> str:
    """:func:`close` without yielding — safe in a ``finally`` / on an
    exception or a client disconnect. Returns the note text."""
    if ws is None or ws.closed:
        return ""
    ws.closed = True
    ws.parked = False
    from aiforge_core.runtime import team_run_life
    from aiforge_core.runtime.parallel_subtasks import _protected
    back, left, stuck = [], [], ""
    try:
        back = _protected.revert(ws.cwd, ws.start_sha or "HEAD")
        left = seal(ws.cwd, "aiforge: edits left by the team run")
    except SealError as exc:
        stuck = str(exc)
    except Exception as exc:  # noqa: BLE001 — cleanup below must still run
        log.debug("close: seal skipped: %s", exc)
    if not stuck and team_run_life.has_pending(ws.cwd, untracked=False):
        stuck = "uncommitted changes remain"
    _protected.clear(ws.cwd)
    with _LOCK:
        _RUNS.pop(ws.cwd, None)
    team_run_life.exclude_release(ws.repo)
    if stuck:
        # Never delete work that exists nowhere else: keep the worktree and
        # the branch and say where they are.
        log.warning("team run %s kept: %s", ws.cwd, stuck)
        return (f"Could not commit the team's last edits ({stuck}), so "
                f"nothing was removed: the work is in `{ws.cwd}` on branch "
                f"`{ws.branch}` — commit it there (e.g. `git -C {ws.cwd} "
                f"commit -am wip`) and merge the branch.")
    try:
        _git(["worktree", "remove", "--force", ws.cwd], ws.repo, 120)
        _git(["worktree", "prune"], ws.repo)
    except Exception as exc:  # noqa: BLE001
        log.debug("close: worktree remove failed: %s", exc)
    import shutil
    shutil.rmtree(ws.run_dir, ignore_errors=True)   # SPEC.md was mirrored
    notes = []
    if back:
        notes.append("Put back read-only file(s) the run changed: "
                     + ", ".join(f"`{b}`" for b in back[:6]) + ".")
    if left:
        notes.append(f"Committed {len(left)} file(s) the run left uncommitted "
                     f"onto `{ws.branch}`.")
    if team_run_life.drop_if_empty(ws):
        notes.append(f"The team made no commits, so branch `{ws.branch}` was "
                     "removed; nothing in your repo changed.")
    elif ws.apply or ws.fresh_repo:
        notes.append(_fast_forward(ws))
    elif not ws.announced:
        notes.append(ws.summary())
    return " ".join(n for n in notes if n)


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


__all__ = ["SealError", "TeamWorkspace", "close", "close_quiet", "dirty_files",
           "ensure_exclude", "for_cwd", "git_env", "init_repo", "open_run",
           "seal", "spec_path", "wants_apply", "write_spec"]
