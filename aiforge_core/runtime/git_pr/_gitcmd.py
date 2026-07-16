"""Low-level git command wrapper + repo/branch resolution helpers.

Split out of the former single-file ``git_pr.py`` (grouped by concern:
excludes / git command layer / PR flow). Layers on the dependency-free
``_excludes`` leaf. No behaviour change — blocks moved verbatim.
"""
from __future__ import annotations

import os
import subprocess

from ._excludes import _is_test_path, log


def _classify_head_diff(repo_root: str) -> tuple[list[str], list[str]]:
    """Return ``(prod_files, test_files)`` changed in HEAD's commit.

    Uses ``git diff-tree`` so it works on the just-created Doer commit
    even before push. Returns two empty lists when nothing changed or
    the command fails — the caller treats that as "skip the guard"
    rather than blocking on a tooling hiccup.
    """
    rc, out, _ = run_git(
        ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
        repo_root,
    )
    if rc != 0 or not out.strip():
        return [], []
    prod, test = [], []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        (test if _is_test_path(line) else prod).append(line)
    return prod, test


def run_git(args: list[str], cwd: str) -> tuple[int, str, str]:
    """Run a git/gh command and capture stdout/stderr.

    Returns ``(returncode, stdout[:1000], stderr[:1000])``. Hard 5-min
    timeout per call so a hung remote can't stall the runner.
    """
    proc = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, timeout=300,
    )
    return proc.returncode, (proc.stdout or "")[:1000], (proc.stderr or "")[:1000]


def _resolve_repo_root() -> str | None:
    """Honour ``AIFORGE_REPO_ROOT`` and confirm it's a git repo.

    Accepts both regular repos (``.git`` is a directory) and worktrees
    (``.git`` is a file containing ``gitdir: ...``). Falls back to
    ``git rev-parse --git-dir`` so any layout git itself accepts also
    works here.
    """
    repo_root = os.path.expanduser(os.environ.get(
        "AIFORGE_REPO_ROOT", "~/aiforge_workspace",
    ))
    dot_git = os.path.join(repo_root, ".git")
    if os.path.isdir(dot_git) or os.path.isfile(dot_git):
        return repo_root
    probe = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=repo_root if os.path.isdir(repo_root) else None,
        check=False, capture_output=True,
    )
    if probe.returncode == 0:
        return repo_root
    log.warning("git_pr.skip: %s is not a git repo", repo_root)
    return None


def _default_base_branch(repo_root: str) -> str:
    """Resolve the upstream base branch. Tries `origin/HEAD` first
    (the GitHub default branch the operator set in repo settings),
    falls back to `origin/master` then `origin/main`. Returns the
    first ref that exists; returns `origin/master` as a sane default
    when nothing resolves (push will fail later with a clear error)."""
    rc, out, _ = run_git(
        ["git", "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"],
        repo_root,
    )
    if rc == 0 and out.strip():
        # `refs/remotes/origin/master` -> `origin/master`
        return out.strip().replace("refs/remotes/", "")
    for candidate in ("origin/master", "origin/main"):
        rc, _, _ = run_git(
            ["git", "rev-parse", "--verify", "--quiet", candidate],
            repo_root,
        )
        if rc == 0:
            return candidate
    return "origin/master"


def _has_unpushed_commits(repo_root: str) -> tuple[bool, str]:
    """``(True, base)`` when HEAD is ahead of the upstream base by at
    least one commit. ``(False, reason)`` otherwise. Used by
    :func:`_has_doer_changes` to detect the Doer-self-committed path:
    PR #22's ``git_commit`` tool lets the Doer commit milestones
    in-loop, leaving the working tree clean by the time
    ``commit_push_open_pr`` runs — without this check, the runner
    short-circuits on ``no_changes`` and the work never gets pushed."""
    base = _default_base_branch(repo_root)
    rc, out, _ = run_git(
        ["git", "rev-list", "--count", f"{base}..HEAD"], repo_root,
    )
    if rc != 0:
        return False, "rev_list_failed"
    try:
        ahead = int((out or "0").strip())
    except ValueError:
        ahead = 0
    if ahead > 0:
        return True, base
    return False, "head_at_base"
