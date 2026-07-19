"""Build the local sync manifest from the markdown memory tree.

Class A (``captures/``, ``compacted/``) is immutable and merges by union on a
content hash. Class B (OKF nodes, tombstones, the compaction lease) is mutable
and carries ``(origin, key, rev, updated_by)`` so two versions can be ordered
without consulting a clock. Exactly one entry is advertised per ``(origin,
key)`` — see ``_dedupe`` — and both halves of an identity must round-trip
``paths.is_addressable`` before it is advertised at all.

The manifest ``hash`` is sha256 of the file bytes. It is unrelated to the
``sha1(title+text)[:6]`` digest embedded in capture filenames, which is a
dedupe device rather than an integrity check.
"""
from __future__ import annotations

import logging
from pathlib import Path

from aiforge_core.memory.sync import _io, merge, paths

_log = logging.getLogger("aiforge.sync")


def _class_a() -> list[dict]:
    out: list[dict] = []
    for sub in ("captures", "compacted"):
        for p in _io.iter_syncable(_io.root() / sub, "*.md"):
            try:
                out.append({"path": _io.rel(p), "hash": _io.sha256_file(p), "cls": "A"})
            except OSError:  # noqa: BLE001 — a file vanishing mid-scan is not fatal
                _log.warning("sync: unreadable capture %s", p)
    return out


def _class_b_entry(p: Path, meta: dict, *, tomb: bool = False) -> dict | None:
    """Build one class B entry — node, tombstone or lease — or None to refuse it.

    The single validation point for the identity space: a rule applied to one
    kind of mutable record applies to all of them. ``meta`` is frontmatter for a
    node (``id``) and the JSON record for a tombstone or the lease (``key``).
    """
    key = str(meta.get("id") or meta.get("key") or "")
    origin = str(meta.get("origin") or "")
    if not key:
        return None

    if not origin and key != paths.LEASE_KEY:
        # Half an identity is not an identity. An unstamped node simply stays
        # local until identity.stamp() writes it again; a tombstone would be
        # worse — ("" , key) is shared by every peer, so one peer's tombstone
        # would overwrite another's. The lease is the sole exception: a
        # mesh-wide singleton addressed by its reserved key, with no origin.
        return None
    if not paths.is_addressable(key) or (origin and not paths.is_addressable(origin)):
        _log.warning("sync: unaddressable identity (%s, %s) in %s, skipping",
                     origin, key, p)
        return None

    try:
        entry = {
            "path": _io.rel(p),
            "hash": _io.sha256_file(p),
            "cls": "B",
            "origin": origin,
            "key": key,
            "rev": merge.as_rev(meta.get("rev")),
            "updated_by": str(meta.get("updated_by") or origin),
        }
    except Exception:  # noqa: BLE001 — a bad record is dropped, the manifest survives
        _log.warning("sync: could not describe %s, skipping", p)
        return None
    if tomb:
        entry["tomb"] = True
    return entry


def _entry_for_node(p: Path) -> dict | None:
    from aiforge_core.memory.okf import nodes as _nodes

    try:
        node = _nodes.parse_node(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — a hand-edited node must not break the manifest
        _log.warning("sync: unreadable node %s", p)
        return None
    return _class_b_entry(p, node.get("meta") or {})


def _entry_for_json(p: Path) -> dict | None:
    rec = _io.read_json(p)
    return _class_b_entry(p, rec, tomb=bool(rec.get("tomb")))


def _dedupe(entries: list[dict]) -> list[dict]:
    """One entry per ``(origin, key)`` — the highest ``(rev, updated_by, hash)``.

    The manifest half of the one-winner rule (I1), matching the order
    ``paths.node_paths`` uses to choose an update target. Advertising the same
    identity twice makes a peer compare one file and write the other, so the
    compared file stays stale and the pair flip-flops every round.
    """
    best: dict[tuple[str, str], dict] = {}
    for e in entries:
        ident = (e["origin"], e["key"])
        cur = best.get(ident)
        if cur is None:
            best[ident] = e
            continue
        loser, winner = (cur, e) if _rank(e) > _rank(cur) else (e, cur)
        _log.warning("sync: %s duplicates identity (%s, %s) held by %s, not advertised",
                     loser["path"], ident[0], ident[1], winner["path"])
        best[ident] = winner
    return list(best.values())


def _rank(entry: dict) -> tuple[int, str, str]:
    return (entry["rev"], entry["updated_by"], entry["hash"])


def _class_b() -> list[dict]:
    out: list[dict] = []
    for p in _io.iter_syncable(paths.okf_dir(), "**/*.md"):
        if p.name == "index.md" or p.name.endswith(".conflict.md"):
            # index.md is regenerated locally; sidecars are local-only by design.
            continue
        entry = _entry_for_node(p)
        if entry:
            out.append(entry)
    for p in _io.iter_syncable(paths.tomb_dir(), "**/*.json"):
        entry = _entry_for_json(p)
        if entry:
            out.append(entry)
    lease = paths.lease_path()
    if _io.is_syncable(lease):
        entry = _entry_for_json(lease)
        if entry:
            out.append(entry)
    return _dedupe(out)


def build() -> list[dict]:
    """Full local manifest, sorted by path for stable diffs."""
    return sorted(_class_a() + _class_b(), key=lambda e: e["path"])


def path_for_hash(digest: str) -> Path | None:
    """Resolve an advertised hash back to a file.

    The caller supplies a hash, never a path, and only files in the
    freshly-built manifest are resolvable. What that guarantees is exactly what
    ``build()`` guarantees: every scan runs through ``_io.iter_syncable``, which
    yields real files only, so a symlink planted in the tree is neither
    advertised nor served — its target stays unreachable through this endpoint.
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
