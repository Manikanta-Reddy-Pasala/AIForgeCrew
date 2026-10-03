"""Every chat on a git project works in its own worktree.

Two chats in one project used to edit the same files, in the same folder, at the
same time. A chat now gets a branch and a separate worktree of its own, made
from the project's current HEAD the first time it speaks:

* the chat's ``cwd`` stays the project folder (its identity: memory, lists,
  counts). ``workdir`` is where it RUNS: ``<config>/chat-worktrees/…``, outside
  the repo, so nothing in the user's tree sees a second copy of the code;
* every turn's edits are committed to the chat's branch (``aiforge/chat-<id>-…``);
* the user's folder and checked-out branch are never touched, except when the
  user asks for it: :func:`merge` fast-forwards their branch to the chat's
  commits (rebasing the chat's branch first if their branch moved), and refuses
  while their tree has uncommitted changes;
* deleting the chat removes the worktree and KEEPS the branch, so nothing it
  committed is lost.

ONE worktree per chat, ONE writer (owner rule). This worktree is the chat's only
one in its project, from the first message to the last, in EVERY mode: simple,
plan and team all run in it, so switching mode never makes another. A team /
pipeline turn uses it as its cwd (its commits, merges and repairs land on the
chat's branch; :func:`seal` commits what a writer left) instead of opening a
second per-run worktree (:mod:`team_workspace` only opens one for a DIFFERENT
repo the user names). Subtasks of a turn run one after another in place, never
in worktrees of their own (see ``parallel_subtasks.fan_out_enabled``). Side
tasks share their parent's worktree. Set ``AIFORGE_CHAT_WORKTREES=0`` to turn
it off.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import uuid

from . import team_workspace as _tw

log = logging.getLogger(__name__)

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
_IDENT: dict[str, "dict | None"] = {}

#: ``.aiforge`` is the repo's own state folder; in a worktree it is a link back
#: to the project's, so rules, notes and project memory resolve the same way.
_STATE_DIR = ".aiforge"


def enabled() -> bool:
    return os.environ.get("AIFORGE_CHAT_WORKTREES", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _timeout(name: str, default: float) -> float:
    try:
        return max(5.0, float(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def _lock(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


def _root() -> str:
    from aiforge_core.config.paths import config_dir
    return os.path.join(str(config_dir()), "chat-worktrees")


#: Where a chat's worktree lives inside its project (kept out of git by
#: ``.git/info/exclude``; the same folder ticket and subtask worktrees use).
_IN_REPO = ".aiforge-worktrees"
#: Next to the worktree, outside its git tree: nothing in it can be committed.
_SCRATCH = "scratch"


def _in_repo() -> bool:
    """AIFORGE_CHAT_WORKTREE_PLACE: ``repo`` (default) puts a chat's worktree in
    ``<project>/.aiforge-worktrees/``; ``config`` in AIForge's own folder."""
    return os.environ.get("AIFORGE_CHAT_WORKTREE_PLACE", "repo").strip().lower() \
        not in ("config", "home", "outside")


def _run_dir(repo: str, name: str) -> str:
    if _in_repo() and os.access(repo, os.W_OK):
        return os.path.join(repo, _IN_REPO, name)
    slug = re.sub(r"[^a-z0-9]+", "-", os.path.basename(repo).lower()).strip("-") or "repo"
    key = hashlib.sha1(repo.encode("utf-8"), usedforsecurity=False).hexdigest()[:6]
    return os.path.join(_root(), f"{slug}-{key}", name)


def scratch_dir(cwd: "str | None") -> "str | None":
    """The chat's scratch folder (for a path that is a chat worktree): where
    helper scripts, logs and dumps go that are not part of the change."""
    if not cwd or not is_worktree(cwd) or not _read_meta(str(cwd)):
        return None
    return os.path.join(os.path.dirname(os.path.normpath(str(cwd))), _SCRATCH)


# ── what a path is ───────────────────────────────────────────────────────────

def main_repo_of(path: "str | None") -> "str | None":
    """The main repo a worktree belongs to, from its ``.git`` file alone (no
    subprocess: a project list asks for many paths). ``None`` when ``path`` is
    not a linked worktree."""
    if not path:
        return None
    dotgit = os.path.join(str(path), ".git")
    try:
        if not os.path.isfile(dotgit):
            return None
        with open(dotgit, encoding="utf-8") as fh:
            first = fh.readline().strip()
    except OSError:
        return None
    if not first.startswith("gitdir:"):
        return None
    gitdir = first[len("gitdir:"):].strip()
    marker = os.sep + "worktrees" + os.sep
    if marker not in gitdir:
        return None                       # a submodule's .git file, not a worktree
    common = gitdir.split(marker, 1)[0]
    return os.path.dirname(os.path.normpath(common))


def is_worktree(path: "str | None") -> bool:
    return main_repo_of(path) is not None


# ── git ──────────────────────────────────────────────────────────────────────

def _env(repo: str) -> "dict | None":
    """The process env plus an aiforge commit identity when this host has none
    (never written to the user's git config)."""
    if repo not in _IDENT:
        ident = None
        try:
            p = subprocess.run(["git", "var", "GIT_COMMITTER_IDENT"], cwd=repo,
                               capture_output=True, text=True, timeout=15)
            if p.returncode != 0:
                ident = {"GIT_AUTHOR_NAME": "aiforge", "GIT_COMMITTER_NAME": "aiforge",
                         "GIT_AUTHOR_EMAIL": "aiforge@local",
                         "GIT_COMMITTER_EMAIL": "aiforge@local"}
        except Exception:  # noqa: BLE001
            ident = None
        _IDENT[repo] = ident
    ident = _IDENT[repo]
    return {**os.environ, **ident} if ident else None


def _git(args, cwd: str, timeout: float = 60, repo: "str | None" = None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          timeout=timeout, env=_env(repo or cwd))


def _out(args, cwd: str, timeout: float = 60) -> str:
    try:
        p = _git(args, cwd, timeout)
    except Exception:  # noqa: BLE001
        return ""
    return (p.stdout or "").strip() if p.returncode == 0 else ""


# ── which chats get one ──────────────────────────────────────────────────────

def _session(session_id) -> "dict | None":
    from aiforge_core.runtime import chat_store
    return chat_store.get_session(int(session_id)) if session_id is not None else None


def workdir_of(session: "dict | None") -> "str | None":
    """The chat's worktree when it has one that still exists."""
    wd = (session or {}).get("workdir")
    return wd if wd and os.path.isdir(wd) and is_worktree(wd) else None


def covers(cwd: "str | None", folder: "str | None") -> bool:
    """Does the chat worktree at ``cwd`` stand for ``folder``? True when
    ``folder`` is the worktree's main repo or anything inside it (so a path the
    user pasted from their own checkout needs no second worktree)."""
    repo = main_repo_of(cwd)
    if not repo or not folder:
        return False
    from .team_run_life import fold
    f, r = fold(folder), fold(repo)
    return f == r or f.startswith(r + os.sep)


def eligible(session: "dict | None", mode: str = "simple") -> "str | None":
    """The repo root this chat should get a worktree of, or None.

    Only a FRESH chat opened on a project folder that is a git repo with a
    commit; any mode (``mode`` is kept for callers, it no longer matters). Not a
    side task (it shares its parent's), not a scratch chat, and not when the
    feature is off."""
    if not enabled() or not session:
        return None
    if session.get("parent_id") or workdir_of(session):
        return None
    cwd = session.get("cwd")
    if not cwd or not os.path.isdir(cwd):
        return None
    try:
        from aiforge_core.memory import projects
        from aiforge_core.runtime import repo_ident
        if repo_ident.is_chat_scratch(cwd) or projects.project_path_of(cwd) is None:
            return None
    except Exception:  # noqa: BLE001
        return None
    if is_worktree(cwd) or not os.path.isdir(os.path.join(cwd, ".git")):
        return None
    # Only a fresh chat. One that already has an answer has been working in the
    # project folder, maybe with edits not committed yet; moving it to a clean
    # worktree mid-conversation would hide that work from it.
    try:
        from aiforge_core.runtime import chat_store
        if any(m.get("role") == "assistant"
               for m in chat_store.get_messages(int(session["id"])) or []):
            return None
    except Exception:  # noqa: BLE001
        pass
    if not _out(["rev-parse", "--verify", "-q", "HEAD"], cwd):
        return None                       # nothing to branch from
    return os.path.realpath(cwd)


# ── create ───────────────────────────────────────────────────────────────────

def _meta_path(wt: str) -> str:
    return os.path.join(os.path.dirname(wt), "meta.json")


def _read_meta(wt: str) -> dict:
    try:
        with open(_meta_path(wt), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_meta(wt: str, meta: dict) -> None:
    try:
        with open(_meta_path(wt), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2)
    except OSError as exc:
        log.debug("chat worktree meta not written: %s", exc)


def _prefix(repo: str) -> str:
    """``aiforge``, unless the repo has a branch of that name (which makes every
    ``aiforge/…`` ref impossible)."""
    return _tw._branch_prefix(repo)


def _exclude_state(repo: str) -> None:
    """Keep the ``.aiforge`` link out of ``git status`` (a symlink is not matched
    by the directory pattern ``.aiforge/``). Local to this clone: it is
    ``.git/info/exclude``, never the user's ``.gitignore``."""
    try:
        _tw.ensure_exclude(repo)
        path = _tw.exclude_path(repo)
        if not path:
            return
        have = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
        if _STATE_DIR not in have.splitlines():
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(("" if not have or have.endswith("\n") else "\n")
                         + _STATE_DIR + "\n")
    except OSError as exc:
        log.debug("info/exclude not updated: %s", exc)


def _link_state(repo: str, wt: str) -> None:
    """Point ``<worktree>/.aiforge`` at the project's, unless the repo tracks its
    own ``.aiforge`` (then the checkout already has one)."""
    link = os.path.join(wt, _STATE_DIR)
    if os.path.lexists(link):
        return
    target = os.path.join(repo, _STATE_DIR)
    try:
        os.makedirs(target, exist_ok=True)
        os.symlink(target, link)
    except OSError as exc:
        log.debug("could not link %s: %s", link, exc)


def ensure(session_id) -> "dict | None":
    """Make the chat's worktree if it should have one and has none yet; return
    its :func:`info`. Slow on a big repo (a checkout): callers show progress.
    Raises ``RuntimeError`` with a readable reason when git refuses."""
    sess = _session(session_id)
    if sess is None:
        return None
    if workdir_of(sess):
        return info(sess)
    repo = eligible(sess, "simple")
    if not repo:
        return None
    with _lock(repo):
        sess = _session(session_id) or sess
        if workdir_of(sess):
            return info(sess)
        token = uuid.uuid4().hex[:4]
        branch = f"{_prefix(repo)}/chat-{sess['id']}-{token}"
        _exclude_state(repo)           # before the folder exists in the repo
        run_dir = _run_dir(repo, f"chat-{sess['id']}-{token}")
        wt = os.path.join(run_dir, "work")
        os.makedirs(os.path.join(run_dir, _SCRATCH), exist_ok=True)
        base_sha = _out(["rev-parse", "HEAD"], repo)
        base_branch = _out(["symbolic-ref", "--short", "-q", "HEAD"], repo)
        dirty = _tw.dirty_files(repo)
        try:
            p = _git(["worktree", "add", "-q", "-b", branch, wt, base_sha], repo,
                     _timeout("AIFORGE_CHAT_WORKTREE_ADD_S", 900))
        except subprocess.TimeoutExpired:
            shutil.rmtree(run_dir, ignore_errors=True)
            _git(["worktree", "prune"], repo, 30)
            raise RuntimeError("creating the worktree took too long; the checkout "
                               "is probably very large or on a slow mount")
        if p.returncode != 0:
            shutil.rmtree(run_dir, ignore_errors=True)
            raise RuntimeError("git could not create a worktree: "
                               + (p.stderr or p.stdout or "").strip()[:200])
        _exclude_state(repo)
        _link_state(repo, wt)
        if os.path.isfile(os.path.join(wt, ".gitmodules")):
            try:
                _git(["submodule", "update", "--init", "--recursive"], wt, 300)
            except Exception as exc:  # noqa: BLE001
                log.debug("submodule init skipped: %s", exc)
        _write_meta(wt, {"repo": repo, "branch": branch, "base_branch": base_branch,
                         "base_sha": base_sha, "session_id": sess["id"],
                         "dirty_at_start": dirty})
        from aiforge_core.runtime import chat_store
        chat_store.set_session_workdir(sess["id"], wt)
    return info(_session(session_id))


# ── look ─────────────────────────────────────────────────────────────────────

def _changed(wt: str) -> list[str]:
    try:
        # The same paths a commit would take: caches and the like are not "edits".
        out = _git(["status", "--porcelain", "--", *_tw.seal_pathspecs()], wt).stdout or ""
    except Exception:  # noqa: BLE001
        return []
    files = []
    for ln in out.splitlines():
        rel = ln[3:].strip().split(" -> ")[-1].strip('"')
        if rel and not any(rel == a or rel.startswith(a + "/") for a in _tw._OWN_ARTIFACTS):
            files.append(rel)
    return files


def info(session: "dict | None") -> "dict | None":
    """Where the chat's work is, and what it would take to merge it. None when
    the chat has no worktree."""
    wt = workdir_of(session)
    if not wt:
        return None
    meta = _read_meta(wt)
    repo = meta.get("repo") or main_repo_of(wt) or ""
    branch = _out(["symbolic-ref", "--short", "-q", "HEAD"], wt) or meta.get("branch", "")
    base_branch = meta.get("base_branch", "")
    ahead = 0
    if meta.get("base_sha"):
        n = _out(["rev-list", "--count", f"{meta['base_sha']}..HEAD"], wt)
        ahead = int(n) if n.isdigit() else 0
    uncommitted = _changed(wt)
    main_head = _out(["rev-parse", "HEAD"], repo) if repo else ""
    main_branch = _out(["symbolic-ref", "--short", "-q", "HEAD"], repo) if repo else ""
    moved = bool(main_head) and main_head != meta.get("base_sha")
    return {"path": wt, "repo": repo, "branch": branch, "base_branch": base_branch,
            "base_sha": meta.get("base_sha", ""), "ahead": ahead,
            "uncommitted": uncommitted, "main_branch": main_branch,
            "main_moved": moved, "main_dirty": _tw.dirty_files(repo) if repo else [],
            "dirty_at_start": meta.get("dirty_at_start") or []}


# ── commit ───────────────────────────────────────────────────────────────────

def seal(session_or_path, message: str = "") -> list[str]:
    """Commit what the chat left uncommitted onto its branch. Returns the files
    committed. Never raises: a failed commit leaves the files in the worktree."""
    wt = session_or_path if isinstance(session_or_path, str) else workdir_of(session_or_path)
    if not wt or not os.path.isdir(wt) or not is_worktree(wt):
        return []
    try:
        _untrack_own(wt)
        _git(["add", "-A", "--", *_tw.seal_pathspecs()], wt)
        names = [n for n in _out(["diff", "--cached", "--name-only"], wt).splitlines() if n]
        if not names:
            return []
        msg = " ".join((message or "aiforge: chat edits").split())[:200]
        ok = _commit(wt, msg)
        return names if ok else []
    except Exception as exc:  # noqa: BLE001
        log.warning("chat worktree seal failed in %s: %s", wt, exc)
        return []


#: Written into a workspace by AIForge's own tools; never part of a change.
_OWN_INDEXES = (".codegraph", "graphify-out")


def _untrack_own(wt: str) -> None:
    """Take AIForge's own index folders back out of the chat's branch when an
    earlier turn committed them (they were not excluded then) and the project
    itself does not track them."""
    base = _read_meta(wt).get("base_sha") or ""
    for name in _OWN_INDEXES:
        if not _out(["ls-files", "--", name], wt):
            continue
        if base and _out(["ls-tree", "--name-only", base, "--", name], wt):
            continue                      # the project tracks it: leave it
        _git(["rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", name], wt)


def _commit(wt: str, message: str) -> bool:
    p = _git(["commit", "-q", "-m", message], wt)
    if p.returncode == 0:
        return True
    p = _git(["-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", message], wt)
    return p.returncode == 0


def seal_for_session(session_id, prompt: str = "") -> list[str]:
    sess = _session(session_id)
    if not sess or not workdir_of(sess) or sess.get("parent_id"):
        return []              # a side task shares its parent's tree; the parent commits it
    first = " ".join((prompt or "").split())[:70]
    return seal(sess, f"aiforge chat {sess['id']}: {first}" if first else f"aiforge chat {sess['id']}")


# ── merge back ───────────────────────────────────────────────────────────────

def merge(session_id) -> dict:
    """Bring the chat's commits onto the user's branch. Never leaves the user's
    folder in a half-merged state: fast-forward only, after rebasing the chat's
    own branch if theirs moved."""
    sess = _session(session_id)
    seal_for_session(session_id)
    data = info(sess)
    if not data:
        return {"ok": False, "reason": "no_worktree",
                "message": "This chat has no worktree of its own."}
    repo, wt, branch = data["repo"], data["path"], data["branch"]
    if data["ahead"] == 0:
        return {"ok": True, "merged": 0, "message": "Nothing to merge: this chat has no commits yet."}
    base_branch = data["base_branch"]
    if not base_branch or data["main_branch"] != base_branch:
        return {"ok": False, "reason": "branch_changed",
                "message": f"Your folder is on `{data['main_branch'] or 'a detached HEAD'}`, not "
                           f"`{base_branch or '?'}` where this chat started. Switch back, or merge "
                           f"`{branch}` yourself."}
    if data["main_dirty"]:
        shown = ", ".join(data["main_dirty"][:3]) + (" …" if len(data["main_dirty"]) > 3 else "")
        return {"ok": False, "reason": "main_dirty",
                "message": f"Your folder has uncommitted changes ({shown}). Commit or stash them, "
                           f"then merge `{branch}`."}
    main_head = _out(["rev-parse", "HEAD"], repo)
    if _git(["merge-base", "--is-ancestor", main_head, branch], repo).returncode != 0:
        # Their branch moved on since this chat started: rebase the chat's own
        # branch (in its worktree — the user's folder is not involved).
        r = _git(["rebase", base_branch], wt, 300)
        if r.returncode != 0:
            _git(["rebase", "--abort"], wt)
            return {"ok": False, "reason": "conflict",
                    "message": f"`{base_branch}` has changed in ways that conflict with this chat. "
                               f"Merge `{branch}` yourself, or ask the chat to resolve it."}
    p = _git(["merge", "--ff-only", "-q", branch], repo, 120)
    if p.returncode != 0:
        return {"ok": False, "reason": "not_fast_forward",
                "message": f"Could not fast-forward `{base_branch}`; the work is on `{branch}`."}
    new_head = _out(["rev-parse", "HEAD"], repo)
    meta = _read_meta(wt)
    meta["base_sha"] = new_head
    _write_meta(wt, meta)
    return {"ok": True, "merged": data["ahead"], "branch": branch, "into": base_branch,
            "message": f"Merged {data['ahead']} commit{'s' if data['ahead'] != 1 else ''} from "
                       f"`{branch}` into `{base_branch}`."}


# ── remove ───────────────────────────────────────────────────────────────────

def remove(session_id) -> dict:
    """Drop the chat's worktree; keep its branch. Uncommitted work is committed
    first. A side task shares its parent's worktree and removes nothing."""
    sess = _session(session_id)
    if not sess or sess.get("parent_id"):
        return {"removed": False}
    wt = sess.get("workdir")
    if not wt:
        return {"removed": False}
    from aiforge_core.runtime import chat_store
    out = {"removed": False, "branch": ""}
    if os.path.isdir(wt) and is_worktree(wt):
        seal_for_session(session_id)
        meta = _read_meta(wt)
        repo = meta.get("repo") or main_repo_of(wt) or ""
        out["branch"] = meta.get("branch", "")
        try:
            if repo:
                _git(["worktree", "remove", "--force", wt], repo, 120)
                _git(["worktree", "prune"], repo, 30)
        except Exception as exc:  # noqa: BLE001
            log.warning("worktree remove failed for %s: %s", wt, exc)
        shutil.rmtree(os.path.dirname(wt), ignore_errors=True)
        out["removed"] = not os.path.isdir(wt)
    chat_store.set_session_workdir(sess["id"], None)
    return out


# ── what the agent is told ───────────────────────────────────────────────────

def prompt_note(cwd: "str | None") -> str:
    """One block telling the agent where it works, so it does not edit the
    user's own folder by absolute path."""
    repo = main_repo_of(cwd)
    if not repo:
        return ""
    meta = _read_meta(str(cwd))
    branch = meta.get("branch", "")
    return ("WORKSPACE: this chat works in its own git worktree"
            + (f" on branch `{branch}`" if branch else "") + f" at {cwd}.\n"
            f"The project's own folder, {repo}, belongs to the user and to other chats: do "
            "not read-modify-write files there by absolute path. Make every edit inside "
            "your workspace (relative paths). Your changes are committed to your branch "
            "after each turn and the user merges them when ready."
            + _scratch_note(cwd))


def _scratch_note(cwd) -> str:
    scratch = scratch_dir(cwd)
    if not scratch:
        return ""
    return ("\nEVERY file left in the workspace is committed and reaches the user's "
            "branch. Put only the requested change there. A helper script, a "
            "one-off test driver, a log, a dump, downloaded or generated data that "
            f"you make only to do the work goes in {scratch} (never committed); "
            "delete what you no longer need.")


__all__ = ["enabled", "covers", "main_repo_of", "is_worktree", "workdir_of", "eligible", "ensure",
           "info", "seal", "seal_for_session", "merge", "remove", "prompt_note"]
