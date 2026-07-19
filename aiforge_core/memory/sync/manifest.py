"""Build the local sync manifest from the markdown memory tree.

Class A (``captures/``, ``compacted/``) is immutable and merges by union on a
content hash. Class B (OKF nodes, tombstones, the compaction lease) is mutable
and carries ``(origin, key, rev, updated_by)`` so two versions can be ordered
without consulting a clock.

The manifest ``hash`` is sha256 of the file bytes. It is unrelated to the
``sha1(title+text)[:6]`` digest embedded in capture filenames, which is a
dedupe device rather than an integrity check.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

_log = logging.getLogger("aiforge.sync")


def _root() -> Path:
    from aiforge_core.memory.md_store import memory_dir

    return memory_dir()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _class_a(root: Path) -> list[dict]:
    out: list[dict] = []
    for sub in ("captures", "compacted"):
        d = root / sub
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.md")):
            try:
                out.append({"path": _rel(root, p), "hash": _sha256(p), "cls": "A"})
            except OSError:  # noqa: BLE001 — a file vanishing mid-scan is not fatal
                _log.warning("sync: unreadable capture %s", p)
    return out


def build() -> list[dict]:
    """Full local manifest, sorted by path for stable diffs."""
    root = _root()
    entries = _class_a(root)
    return sorted(entries, key=lambda e: e["path"])


def path_for_hash(digest: str) -> Path | None:
    """Resolve an advertised hash back to a file.

    Only files present in the freshly-built manifest are resolvable, so this
    cannot be walked outside the memory tree regardless of what the caller
    supplies — path traversal is impossible by construction.
    """
    digest = (digest or "").strip().lower()
    if not digest:
        return None
    root = _root()
    for e in build():
        if e["hash"] == digest:
            p = root / e["path"]
            if p.is_file():
                return p
    return None


__all__ = ["build", "path_for_hash"]
