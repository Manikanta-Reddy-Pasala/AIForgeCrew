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

import logging
from pathlib import Path

from aiforge_core.memory.sync import _io, paths

_log = logging.getLogger("aiforge.sync")


def _class_a() -> list[dict]:
    out: list[dict] = []
    for sub in ("captures", "compacted"):
        d = _io.root() / sub
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.md")):
            if not _io.is_syncable(p):
                # glob follows symlinks; advertising one would serve its target.
                continue
            try:
                out.append({"path": _io.rel(p), "hash": _io.sha256_file(p), "cls": "A"})
            except OSError:  # noqa: BLE001 — a file vanishing mid-scan is not fatal
                _log.warning("sync: unreadable capture %s", p)
    return out


def _entry_for_node(p: Path) -> dict | None:
    from aiforge_core.memory.okf import nodes as _nodes

    try:
        node = _nodes.parse_node(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — a hand-edited node must not break the manifest
        _log.warning("sync: unreadable node %s", p)
        return None
    meta = node.get("meta") or {}
    origin = str(meta.get("origin") or "")
    key = str(meta.get("id") or "")
    if not origin or not key:
        # Not yet stamped by identity.stamp(); stays local until it is written again.
        return None
    return {
        "path": _io.rel(p),
        "hash": _io.sha256_file(p),
        "cls": "B",
        "origin": origin,
        "key": key,
        "rev": int(meta.get("rev") or 0),
        "updated_by": str(meta.get("updated_by") or origin),
    }


def _entry_for_json(p: Path) -> dict | None:
    rec = _io.read_json(p)
    if not rec.get("key"):
        return None
    entry = {
        "path": _io.rel(p),
        "hash": _io.sha256_file(p),
        "cls": "B",
        "origin": str(rec.get("origin") or ""),
        "key": str(rec.get("key")),
        "rev": int(rec.get("rev") or 0),
        "updated_by": str(rec.get("updated_by") or ""),
    }
    if rec.get("tomb"):
        entry["tomb"] = True
    return entry


def _class_b() -> list[dict]:
    okf = paths.okf_dir()
    if not okf.is_dir():
        return []
    out: list[dict] = []
    for p in sorted(okf.rglob("*.md")):
        if p.name == "index.md" or p.name.endswith(".conflict.md"):
            # index.md is regenerated locally; sidecars are local-only by design.
            continue
        entry = _entry_for_node(p)
        if entry:
            out.append(entry)
    tomb = paths.tomb_dir()
    if tomb.is_dir():
        for p in sorted(tomb.rglob("*.json")):
            entry = _entry_for_json(p)
            if entry:
                out.append(entry)
    lease = paths.lease_path()
    if lease.is_file():
        entry = _entry_for_json(lease)
        if entry:
            out.append(entry)
    return out


def build() -> list[dict]:
    """Full local manifest, sorted by path for stable diffs."""
    return sorted(_class_a() + _class_b(), key=lambda e: e["path"])


def path_for_hash(digest: str) -> Path | None:
    """Resolve an advertised hash back to a file.

    Only files present in the freshly-built manifest are resolvable, so this
    cannot be walked outside the memory tree regardless of what the caller
    supplies — path traversal is impossible by construction.
    """
    digest = (digest or "").strip().lower()
    if not digest:
        return None
    root = _io.root()
    for e in build():
        if e["hash"] == digest:
            p = root / e["path"]
            if p.is_file():
                return p
    return None


__all__ = ["build", "path_for_hash"]
