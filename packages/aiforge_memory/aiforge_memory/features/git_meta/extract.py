"""Git metadata extractor for the Repo node.

Reads (head_sha, branch, default_branch, remote_url, dirty) via subprocess
git calls. All fields degrade gracefully: missing git CLI / non-repo /
detached HEAD all return empty strings or False, never raise.

Public surface:
    git_meta.read(repo_path) -> GitMeta
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class GitMeta:
    head_sha: str = ""
    branch: str = ""             # current checked-out branch (HEAD ref)
    default_branch: str = ""     # remote HEAD target (origin/main, origin/master, ...)
    remote_url: str = ""         # origin fetch URL
    dirty: bool = False          # uncommitted changes present


def read(repo_path: str | Path) -> GitMeta:
    repo_path = str(Path(repo_path).resolve())
    return GitMeta(
        head_sha=_git(repo_path, "rev-parse", "HEAD"),
        branch=_git(repo_path, "rev-parse", "--abbrev-ref", "HEAD"),
        default_branch=_default_branch(repo_path),
        remote_url=_git(repo_path, "config", "--get", "remote.origin.url"),
        dirty=_dirty(repo_path),
    )


def _git(cwd: str, *args: str, timeout: int = 5) -> str:
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if r.returncode != 0:
        return ""
    return (r.stdout or "").strip()


def _default_branch(cwd: str) -> str:
    """Best-effort: ``git symbolic-ref refs/remotes/origin/HEAD`` returns
    ``refs/remotes/origin/main``; we strip the prefix. Falls back to
    common names (``main``, ``master``) by checking remote refs."""
    raw = _git(cwd, "symbolic-ref", "refs/remotes/origin/HEAD")
    if raw:
        # ``refs/remotes/origin/main`` -> ``main``
        return raw.rsplit("/", 1)[-1]
    # Fallback: probe known names
    for name in ("main", "master"):
        if _git(cwd, "rev-parse", "--verify", f"refs/remotes/origin/{name}"):
            return name
    return ""


def _dirty(cwd: str) -> bool:
    """Any tracked-file modifications or staged changes -> dirty=True.
    Untracked files alone do not count as dirty (matches `git status`)."""
    out = _git(cwd, "status", "--porcelain=v1", "-uno")
    return bool(out)
