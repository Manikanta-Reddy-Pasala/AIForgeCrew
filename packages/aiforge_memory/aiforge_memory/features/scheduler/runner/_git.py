"""Git helpers — fetch + ff-only pull (poll-decide)."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


# ─── Git helpers (poll-decide) ────────────────────────────────────────

@dataclass
class FetchOutcome:
    fetched: bool                  # `git fetch` succeeded
    pulled: bool                   # `git pull --ff-only` succeeded
    behind: int                    # commits behind upstream BEFORE pull
    skipped_reason: str = ""       # 'dirty' | 'no_upstream' | ''


def fetch_and_maybe_pull(
    repo_path: str | Path, *, do_pull: bool = True,
) -> FetchOutcome:
    """git fetch; report ahead/behind; ff-only pull if behind & clean.

    - Refuses to pull when working tree is dirty.
    - Refuses to pull when upstream is not configured.
    - ff-only — won't merge or rebase divergent histories.
    """
    cwd = str(Path(repo_path).resolve())

    fetch_ok = _git_run(cwd, "fetch", "--quiet").returncode == 0
    if not fetch_ok:
        return FetchOutcome(False, False, 0)

    behind = _commits_behind(cwd)
    if behind <= 0:
        return FetchOutcome(True, False, 0)

    if not do_pull:
        return FetchOutcome(True, False, behind)

    if _is_dirty(cwd):
        return FetchOutcome(True, False, behind, skipped_reason="dirty")

    if not _has_upstream(cwd):
        return FetchOutcome(True, False, behind, skipped_reason="no_upstream")

    pull = _git_run(cwd, "pull", "--ff-only", "--quiet")
    return FetchOutcome(True, pull.returncode == 0, behind)


def _git_run(cwd: str, *args: str, timeout: int = 30):
    return subprocess.run(
        ["git", *args],
        cwd=cwd, capture_output=True, text=True, timeout=timeout,
    )


def _commits_behind(cwd: str) -> int:
    """Count of commits HEAD is behind @{u}. 0 if up-to-date or unknown."""
    r = _git_run(cwd, "rev-list", "--count", "HEAD..@{u}")
    if r.returncode != 0:
        return 0
    try:
        return int((r.stdout or "0").strip())
    except ValueError:
        return 0


def _is_dirty(cwd: str) -> bool:
    r = _git_run(cwd, "status", "--porcelain=v1", "-uno")
    return bool((r.stdout or "").strip())


def _has_upstream(cwd: str) -> bool:
    return _git_run(
        cwd, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}",
    ).returncode == 0
