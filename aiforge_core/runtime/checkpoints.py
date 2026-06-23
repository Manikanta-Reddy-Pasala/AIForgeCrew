"""Workspace checkpoints — snapshot + restore the chat working tree.

Cline/opencode snapshot the workspace at each step so you can undo an
agent's edits wholesale. We do the same with a non-intrusive git trick:
stage the whole worktree (tracked + untracked) into a TEMP index, write a
tree, and ``commit-tree`` it onto a hidden ref — without touching the real
index, HEAD, or working tree. Restore checks those files back out.

Snapshots live on ``refs/aiforge-ckpt/<n>``; labels/timestamps are kept in
a sidecar JSON (``~/.aiforge/checkpoints/<repo>.json``). Everything is
best-effort and a no-op outside a git repo.

Restore note: restore brings tracked snapshot paths back to their snapshot
content. Files CREATED after the checkpoint are left in place (we never
auto-delete) — surfaced in the result so the caller can decide.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path


def _git(cwd: str, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
        env={**os.environ, **(env or {})}, check=False)


def _is_repo(cwd: str) -> bool:
    return _git(cwd, "rev-parse", "--git-dir").returncode == 0


def _has_head(cwd: str) -> bool:
    return _git(cwd, "rev-parse", "--verify", "-q", "HEAD").returncode == 0


def _repo_key(cwd: str) -> str:
    root = os.environ.get("AIFORGE_WORKSPACE_DIR") or cwd
    return os.path.basename(os.path.abspath(root).rstrip(os.sep)) or "repo"


def _sidecar(cwd: str) -> Path:
    d = Path(os.path.expanduser(
        os.environ.get("AIFORGE_CHECKPOINT_DIR", "~/.aiforge/checkpoints")))
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{_repo_key(cwd)}.json"


def _load(cwd: str) -> list[dict]:
    p = _sidecar(cwd)
    if p.exists():
        try:
            return json.loads(p.read_text()) or []
        except Exception:  # noqa: BLE001
            return []
    return []


def _save(cwd: str, rows: list[dict]) -> None:
    try:
        _sidecar(cwd).write_text(json.dumps(rows, indent=2))
    except Exception:  # noqa: BLE001
        pass


def snapshot(cwd: str, label: str = "", when: str = "") -> dict:
    """Snapshot the whole working tree to a hidden ref. Non-intrusive.

    ``when`` is an ISO timestamp the caller stamps (kept out of this module
    so it stays deterministic/testable). Returns ``{ok, sha, label}`` or
    ``{ok: False, error}`` outside a git repo / on failure."""
    if not _is_repo(cwd):
        return {"ok": False, "error": "not_a_git_repo"}
    tmp = tempfile.NamedTemporaryFile(prefix="aiforge-ckpt-idx-", delete=False)
    tmp.close()
    try:
        env = {"GIT_INDEX_FILE": tmp.name}
        if _has_head(cwd):
            _git(cwd, "read-tree", "HEAD", env=env)
        # Stage everything currently in the worktree (tracked + untracked).
        add = _git(cwd, "add", "-A", env=env)
        if add.returncode != 0:
            return {"ok": False, "error": f"git add failed: {add.stderr[:200]}"}
        tree = _git(cwd, "write-tree", env=env)
        if tree.returncode != 0:
            return {"ok": False, "error": f"write-tree failed: {tree.stderr[:200]}"}
        tree_sha = tree.stdout.strip()
        msg = f"aiforge-checkpoint: {label or 'snapshot'}"
        ct_args = ["commit-tree", tree_sha, "-m", msg]
        if _has_head(cwd):
            head = _git(cwd, "rev-parse", "HEAD").stdout.strip()
            ct_args = ["commit-tree", tree_sha, "-p", head, "-m", msg]
        ct = _git(cwd, *ct_args)
        if ct.returncode != 0:
            return {"ok": False, "error": f"commit-tree failed: {ct.stderr[:200]}"}
        sha = ct.stdout.strip()
        rows = _load(cwd)
        ref = f"refs/aiforge-ckpt/{len(rows) + 1}"
        _git(cwd, "update-ref", ref, sha)
        row = {"sha": sha, "ref": ref, "label": label or "snapshot", "when": when}
        rows.append(row)
        _save(cwd, rows)
        return {"ok": True, **row}
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def list_checkpoints(cwd: str) -> list[dict]:
    """Most-recent-first list of this repo's checkpoints."""
    return list(reversed(_load(cwd)))


def restore(cwd: str, sha: str) -> dict:
    """Restore tracked snapshot paths to their checkpoint content.

    Files created AFTER the checkpoint are left untouched (never
    auto-deleted) and reported in ``left_in_place``."""
    if not _is_repo(cwd):
        return {"ok": False, "error": "not_a_git_repo"}
    if not sha or _git(cwd, "cat-file", "-e", sha).returncode != 0:
        return {"ok": False, "error": "unknown_checkpoint"}
    # Files in the worktree now but NOT in the snapshot tree → would-orphan.
    now = set(_git(cwd, "ls-files").stdout.split())
    snap = set(_git(cwd, "ls-tree", "-r", "--name-only", sha).stdout.split())
    left = sorted(now - snap)
    co = _git(cwd, "checkout", sha, "--", ".")
    if co.returncode != 0:
        return {"ok": False, "error": f"checkout failed: {co.stderr[:200]}"}
    return {"ok": True, "restored": sha, "left_in_place": left}


__all__ = ["snapshot", "list_checkpoints", "restore"]
