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
    # Key the sidecar off the session's REAL ``cwd`` (its git toplevel, or the
    # cwd basename) — NOT the global ``AIFORGE_WORKSPACE_DIR`` env. Refs are
    # created in ``cwd``; on deploy hosts where the env points elsewhere,
    # keying off the env would file metadata under the wrong sidecar (restore
    # fails / cross-repo collision).
    top = _git(cwd, "rev-parse", "--show-toplevel").stdout.strip()
    root = top or cwd
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


def _build_snapshot_tree(cwd: str, env: dict) -> "tuple[str | None, dict]":
    """Stage the whole worktree into the temp index and write a tree. Returns
    ``(tree_sha, {})`` on success, or ``(None, error_dict)``."""
    # Initialize the temp index: from HEAD if it exists, else an EMPTY index (a
    # brand-new workspace has no commit yet — without this the zero-byte temp
    # file isn't a valid index and ``git add -A`` fails "index file smaller
    # than expected").
    if _has_head(cwd):
        _git(cwd, "read-tree", "HEAD", env=env)
    else:
        _git(cwd, "read-tree", "--empty", env=env)
    add = _git(cwd, "add", "-A", env=env)
    if add.returncode != 0:
        return None, {"ok": False, "error": f"git add failed: {add.stderr[:200]}"}
    tree = _git(cwd, "write-tree", env=env)
    if tree.returncode != 0:
        return None, {"ok": False, "error": f"write-tree failed: {tree.stderr[:200]}"}
    return tree.stdout.strip(), {}


def _commit_snapshot_tree(cwd: str, tree_sha: str, label: str) -> "tuple[str | None, dict]":
    """Commit ``tree_sha`` (parented on HEAD when there is one). Returns
    ``(commit_sha, {})`` or ``(None, error_dict)``."""
    msg = f"aiforge-checkpoint: {label or 'snapshot'}"
    ct_args = ["commit-tree", tree_sha, "-m", msg]
    if _has_head(cwd):
        head = _git(cwd, "rev-parse", "HEAD").stdout.strip()
        ct_args = ["commit-tree", tree_sha, "-p", head, "-m", msg]
    ct = _git(cwd, *ct_args)
    if ct.returncode != 0:
        return None, {"ok": False, "error": f"commit-tree failed: {ct.stderr[:200]}"}
    return ct.stdout.strip(), {}


def _next_checkpoint_ref(cwd: str) -> str:
    """The next ``refs/aiforge-ckpt/N`` ref, numbered from EXISTING refs (not the
    sidecar length — a lost/corrupt sidecar would reuse an index and clobber a
    live checkpoint)."""
    existing = _git(cwd, "for-each-ref", "--format=%(refname)",
                    "refs/aiforge-ckpt/").stdout.split()
    max_n = 0
    for r in existing:
        try:
            max_n = max(max_n, int(r.rsplit("/", 1)[-1]))
        except (ValueError, IndexError):
            continue
    return f"refs/aiforge-ckpt/{max_n + 1}"


def snapshot(cwd: str, label: str = "", when: str = "") -> dict:
    """Snapshot the whole working tree to a hidden ref. Non-intrusive.

    ``when`` is an ISO timestamp the caller stamps (kept out of this module so it
    stays deterministic/testable). Returns ``{ok, sha, label}`` or
    ``{ok: False, error}`` outside a git repo / on failure."""
    if not _is_repo(cwd):
        return {"ok": False, "error": "not_a_git_repo"}
    tmp = tempfile.NamedTemporaryFile(prefix="aiforge-ckpt-idx-", delete=False)
    tmp.close()
    try:
        env = {"GIT_INDEX_FILE": tmp.name}
        tree_sha, err = _build_snapshot_tree(cwd, env)
        if tree_sha is None:
            return err
        sha, err = _commit_snapshot_tree(cwd, tree_sha, label)
        if sha is None:
            return err
        ref = _next_checkpoint_ref(cwd)
        _git(cwd, "update-ref", ref, sha)
        row = {"sha": sha, "ref": ref, "label": label or "snapshot", "when": when}
        rows = _load(cwd)
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


def _worktree_vs_snapshot(cwd: str, sha: str) -> "list[str]":
    """Paths in the worktree now (tracked + untracked-not-ignored) but NOT in the
    snapshot tree — the would-orphan set. NUL-delimited so paths with spaces or
    newlines don't split wrong."""
    now = {p for p in _git(cwd, "ls-files", "-z").stdout.split("\0") if p}
    now |= {p for p in _git(cwd, "ls-files", "-o", "--exclude-standard", "-z")
            .stdout.split("\0") if p}
    snap = {p for p in _git(cwd, "ls-tree", "-r", "--name-only", "-z", sha)
            .stdout.split("\0") if p}
    return sorted(now - snap)


def _restore_worktree(cwd: str, sha: str, targets: list) -> "dict | None":
    """Restore ``targets`` from ``sha`` into the worktree (index untouched — the
    non-intrusive ``git restore --worktree`` form), falling back to ``git
    checkout`` on older git. Returns an error dict on failure, else None."""
    co = _git(cwd, "restore", "--source", sha, "--worktree", "--", *targets)
    if co.returncode != 0:
        co = _git(cwd, "checkout", sha, "--", *targets)
        if co.returncode != 0:
            return {"ok": False, "error": f"restore failed: {co.stderr[:200]}"}
    return None


def _delete_orphans(cwd: str, left: list, paths: "list[str] | None") -> list:
    """Delete would-orphan files (restricted to ``paths`` when given) so the tree
    exactly matches the snapshot. Returns the deleted paths."""
    def _under(p: str) -> bool:
        if not paths:
            return True
        return any(p == t or p.startswith(t.rstrip("/") + "/") for t in paths)

    deleted: list[str] = []
    for rel in left:
        if not _under(rel):
            continue
        try:
            os.unlink(os.path.join(cwd, rel))
            deleted.append(rel)
        except OSError:
            pass
    return deleted


def restore(cwd: str, sha: str, *, paths: list[str] | None = None,
            delete_orphans: bool = False) -> dict:
    """Restore snapshot paths to their checkpoint content.

    Granularity (Cline-parity): ``paths=None`` restores the whole tracked
    snapshot; ``paths=[...]`` restores ONLY those paths. Orphans (files created
    after the checkpoint): ``delete_orphans=False`` leaves them and reports them
    in ``left_in_place``; ``delete_orphans=True`` deletes them (only under
    ``paths`` when given) for an exact-match restore.

    Returns ``{ok, restored, left_in_place, deleted}``."""
    if not _is_repo(cwd):
        return {"ok": False, "error": "not_a_git_repo"}
    if not sha or _git(cwd, "cat-file", "-e", sha).returncode != 0:
        return {"ok": False, "error": "unknown_checkpoint"}
    left = _worktree_vs_snapshot(cwd, sha)
    err = _restore_worktree(cwd, sha, list(paths) if paths else ["."])
    if err is not None:
        return err
    deleted: list[str] = []
    if delete_orphans:
        deleted = _delete_orphans(cwd, left, paths)
        left = [p for p in left if p not in set(deleted)]
    return {"ok": True, "restored": sha, "left_in_place": left,
            "deleted": deleted}
