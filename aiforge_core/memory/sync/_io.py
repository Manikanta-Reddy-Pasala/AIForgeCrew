"""Disk primitives shared by every sync module.

This is the single place that knows how to find the memory tree, hash a file,
and write one without risking a truncated result. Modules that need any of
those import them from here rather than growing their own copy.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path

from aiforge_core.config import _atomic

_log = logging.getLogger("aiforge.sync")

# Resolved memory roots, keyed by the env that selects them. ``memory_dir()``
# creates the directory, and ``rel()`` calls ``root()`` once per manifest entry,
# so without this a read-only manifest build issues an mkdir syscall per file.
_ROOTS: dict[tuple[str, str], Path] = {}


def root() -> Path:
    """The markdown memory tree — the source of truth this whole feature syncs.

    Cached per selecting-env, not per process: tests (and a future multi-tree
    host) swap ``AIFORGE_MEMORY_MD_DIR`` between calls, and a flat module-level
    cache would serve one peer's tree to another. The cached value is only the
    resolved path — every writer still mkdirs its own parents — so a root
    deleted underneath us costs nothing.
    """
    from aiforge_core.memory.md_store import memory_dir

    key = (os.environ.get("AIFORGE_MEMORY_MD_DIR") or "",
           os.environ.get("AIFORGE_CONFIG_DIR") or "")
    cached = _ROOTS.get(key)
    if cached is None:
        cached = _ROOTS[key] = memory_dir()
    return cached


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rel(path: Path) -> str:
    """Path relative to the tree root, in the posix form the manifest uses."""
    return path.relative_to(root()).as_posix()


def safe_target(relative: str) -> Path | None:
    """Resolve a manifest-supplied path inside the tree, or None if it escapes.

    A peer supplies these strings, so they are untrusted input: anything that
    resolves to the root itself or outside it is refused rather than clamped.
    """
    if not relative:
        return None
    base = root().resolve()
    try:
        target = (base / relative).resolve()
    except (OSError, ValueError):  # a hostile path must not raise
        return None
    if target == base or base not in target.parents:
        _log.warning("sync: rejected out-of-tree path %s", relative)
        return None
    return target


def is_syncable(path: Path) -> bool:
    """True for a real file we are willing to advertise to a peer.

    Symlinks are refused. ``Path.glob`` follows them, so a symlink planted under
    ``captures/`` would otherwise be listed in the manifest and its *target*
    served over ``/blob`` — turning a read-only sync endpoint into a way to read
    arbitrary files outside the memory tree.
    """
    return path.is_file() and not path.is_symlink()


def _hidden_below(directory: Path, path: Path) -> bool:
    """True when ``path`` sits in — or is — a dotted entry *below* ``directory``.

    ``Path.glob("**/*.md")`` descends into dot-directories, so ``okf/.trash/``
    (the reversible bin ``okf.author`` moves a node the operator classified as
    noise into) was advertised in the manifest, served over ``/blob``, re-planted
    on every peer and folded into the mesh. A dotted name below a scanned root is
    this machine's own bookkeeping, never knowledge.

    Only components *below* ``directory`` are judged, so a root that is itself
    dotted still scans: ``okf/.tomb/`` is passed in as the directory and its
    tombstones (``<origin>/<key>.json``) keep being advertised.
    """
    return any(part.startswith(".") for part in path.relative_to(directory).parts)


def iter_syncable(directory: Path, pattern: str) -> Iterator[Path]:
    """Every real file under ``directory`` matching ``pattern``, in path order.

    The single scan used by both the manifest and the layout rule, so the
    symlink refusal above — and the dot-directory refusal below it — applies to
    every record class rather than only the one whose scan remembered to ask.
    """
    if not directory.is_dir():
        return
    for p in sorted(directory.glob(pattern)):
        if is_syncable(p) and not _hidden_below(directory, p):
            yield p


def write_atomic(target: Path, body: bytes) -> None:
    """Publish ``body`` at ``target`` as a single visible step.

    The sync-side name for the repo-wide primitive in ``config._atomic`` — kept
    because every sync module already reaches for its disk verbs here. The
    guarantees (one whole body under concurrent writers, fsynced content, no
    temp litter, and *no* durability for the rename itself) are documented
    there; this is not a second implementation.

    Mode 0600 is passed explicitly: the memory tree holds this machine's
    knowledge and every peer's synced notes, so it stays owner-only rather than
    following the umask. Without this the shared helper would widen these files
    to 0644 to match the config call sites it also serves.
    """
    _atomic.write_bytes(target, body, mode=0o600)


def read_json(path: Path) -> dict:
    """Load a JSON record, or {} if it is absent or unreadable.

    Soft-fail is correct here: a corrupt marker file must degrade the node, not
    stop it. The caller treats {} as "no record".
    """
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — hand-edited or truncated JSON must not raise
        _log.warning("sync: unreadable json %s, treating as empty", path)
        return {}
    return data if isinstance(data, dict) else {}


def write_json(path: Path, record: dict) -> None:
    write_atomic(path, json.dumps(record, indent=2).encode("utf-8"))


def read_node_meta(path: Path) -> dict:
    """Frontmatter of an OKF node, or {} if it cannot be read or parsed.

    The one place that turns a node file into its identity fields. Both callers
    — the manifest builder and the layout rule — must treat a hand-edited node
    as "no identity" rather than raise, and treating it two different ways is
    how the two ends of the one-winner rule drift apart.
    """
    from aiforge_core.memory.okf import nodes as _nodes

    try:
        meta = _nodes.parse_node(path.read_text(encoding="utf-8")).get("meta")
    except Exception:  # noqa: BLE001 — a hand-edited node must not break a scan
        _log.warning("sync: unreadable node %s", path)
        return {}
    return meta if isinstance(meta, dict) else {}


__all__ = ["root", "sha256_file", "rel", "safe_target", "is_syncable",
           "iter_syncable", "write_atomic", "read_json", "write_json",
           "read_node_meta"]
