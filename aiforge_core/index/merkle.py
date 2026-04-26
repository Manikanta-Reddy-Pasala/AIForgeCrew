"""Merkle hash tree for incremental codebase indexing.

KISS: file → folder → root SHA-256 chain. Persisted in SQLite per
worktree under ``$AIFORGE_MERKLE_DIR/<repo_name>.db``. Root hash
survives restart, enables remote diff (push only files whose folder
hash changed).

Two operations:
- :func:`build(root)` — full walk, returns root hash, persists every
  file + folder hash. Idempotent.
- :func:`diff(root, prev_root_sha)` — return list of changed file
  paths since the prior root hash. Empty list = nothing changed,
  caller skips reindex entirely.

Pruned dirs: ``.git``, ``node_modules``, ``target``, ``build``,
``dist``, ``.venv``, ``__pycache__``, ``.aider.tags.cache.v4``.

Public surface:
- ``build(root) -> str``
- ``diff(root, prev_root_sha) -> list[str]``
- ``current_root(root) -> str | None``
- ``forget(root)`` — drop the cache (force full rebuild next call)
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path
from typing import Iterator


from aiforge_core.index.noise import EXCLUDE_DIRS as _PRUNE  # shared filter

_ALLOWED_EXTS = frozenset({
    ".java", ".py", ".ts", ".tsx", ".js", ".jsx", ".kt", ".go",
    ".rs", ".rb", ".scala", ".cs", ".cpp", ".c", ".h", ".hpp",
    ".yml", ".yaml", ".xml", ".json", ".md", ".sh", ".sql",
    ".tf", ".gradle", ".pom",
})


def build(root: str) -> str:
    """Walk + hash + persist. Returns the new root hash."""
    root_path = Path(root).resolve()
    if not root_path.is_dir():
        raise ValueError(f"not a directory: {root}")

    conn = _conn(root_path.name)
    _ensure_schema(conn)

    file_hashes: dict[str, str] = {}
    folder_files: dict[str, list[str]] = {}

    for file_path in _walk(root_path):
        rel = str(file_path.relative_to(root_path))
        try:
            digest = _file_sha(file_path)
        except Exception:
            continue
        file_hashes[rel] = digest
        folder = str(Path(rel).parent) or "."
        folder_files.setdefault(folder, []).append(rel)

    # Folder hashes = sha256(sorted child file hashes + sorted child folder hashes)
    folder_hashes = _compute_folder_hashes(folder_files, file_hashes)
    root_hash = folder_hashes.get(".", _empty_sha())

    _persist(conn, file_hashes, folder_hashes, root_hash)
    conn.close()
    return root_hash


def diff(root: str, prev_root_sha: str | None) -> list[str]:
    """Return changed-file paths since ``prev_root_sha``.

    KISS: walks current state, compares per-file hashes against
    persisted snapshot. Returns paths whose hash differs OR which
    are net-new. Empty list when current root == prev_root_sha.
    """
    if prev_root_sha and prev_root_sha == current_root(root):
        return []
    root_path = Path(root).resolve()
    conn = _conn(root_path.name)
    _ensure_schema(conn)
    prior = dict(conn.execute("SELECT path, sha FROM file_hashes"))

    changed: list[str] = []
    seen: set[str] = set()
    for file_path in _walk(root_path):
        rel = str(file_path.relative_to(root_path))
        seen.add(rel)
        try:
            cur_sha = _file_sha(file_path)
        except Exception:
            continue
        if prior.get(rel) != cur_sha:
            changed.append(rel)
    # Deletions = paths in prior but no longer present.
    for old_path in prior:
        if old_path not in seen:
            changed.append(old_path)
    conn.close()
    return changed


def current_root(root: str) -> str | None:
    """Return the persisted root hash, or None when never built."""
    root_path = Path(root).resolve()
    conn = _conn(root_path.name)
    _ensure_schema(conn)
    row = conn.execute(
        "SELECT sha FROM folder_hashes WHERE path='.'"
    ).fetchone()
    conn.close()
    return row[0] if row else None


def forget(root: str) -> None:
    """Drop the cache for this root."""
    db = _db_path(Path(root).resolve().name)
    if db.exists():
        db.unlink()


# ───────── helpers ────────────────────────────────────────────────


def _walk(root: Path) -> Iterator[Path]:
    for cur, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _PRUNE]
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext and ext not in _ALLOWED_EXTS:
                continue
            yield Path(cur) / fname


def _file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65_536), b""):
            h.update(chunk)
    return h.hexdigest()


def _empty_sha() -> str:
    return hashlib.sha256(b"").hexdigest()


def _compute_folder_hashes(
    folder_files: dict[str, list[str]],
    file_hashes: dict[str, str],
) -> dict[str, str]:
    """Bottom-up fold. Folder hash = sha256(sorted children hashes)."""
    # Build folder tree explicitly so parents can hash child folders.
    all_folders = set(folder_files.keys())
    for folder in list(all_folders):
        parts = Path(folder).parts
        for i in range(len(parts)):
            all_folders.add(str(Path(*parts[:i + 1])) or ".")
    all_folders.add(".")

    children_of: dict[str, list[str]] = {f: [] for f in all_folders}
    for f in all_folders:
        if f == ".":
            continue
        parent = str(Path(f).parent) or "."
        if parent in children_of and f not in children_of[parent]:
            children_of[parent].append(f)

    out: dict[str, str] = {}

    def _hash(folder: str) -> str:
        if folder in out:
            return out[folder]
        h = hashlib.sha256()
        for fpath in sorted(folder_files.get(folder, [])):
            h.update(file_hashes[fpath].encode())
        for child in sorted(children_of.get(folder, [])):
            h.update(_hash(child).encode())
        out[folder] = h.hexdigest()
        return out[folder]

    for folder in all_folders:
        _hash(folder)
    return out


def _persist(
    conn: sqlite3.Connection,
    file_hashes: dict[str, str],
    folder_hashes: dict[str, str],
    root_hash: str,
) -> None:
    conn.execute("DELETE FROM file_hashes")
    conn.execute("DELETE FROM folder_hashes")
    conn.executemany(
        "INSERT INTO file_hashes(path, sha) VALUES (?,?)",
        list(file_hashes.items()),
    )
    conn.executemany(
        "INSERT INTO folder_hashes(path, sha) VALUES (?,?)",
        list(folder_hashes.items()),
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('root', ?)",
        (root_hash,),
    )
    conn.commit()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS file_hashes ("
        " path TEXT PRIMARY KEY, sha TEXT);"
        "CREATE TABLE IF NOT EXISTS folder_hashes ("
        " path TEXT PRIMARY KEY, sha TEXT);"
        "CREATE TABLE IF NOT EXISTS meta ("
        " key TEXT PRIMARY KEY, value TEXT);"
    )


def _db_path(repo_name: str) -> Path:
    base = Path(os.environ.get(
        "AIFORGE_MERKLE_DIR",
        os.path.expanduser("~/.aiforge/merkle"),
    ))
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{repo_name}.db"


def _conn(repo_name: str) -> sqlite3.Connection:
    return sqlite3.connect(str(_db_path(repo_name)))
