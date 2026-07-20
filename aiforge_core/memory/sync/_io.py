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


def iter_syncable(directory: Path, pattern: str) -> Iterator[Path]:
    """Every real file under ``directory`` matching ``pattern``, in path order.

    The single scan used by both the manifest and the layout rule, so the
    symlink refusal above applies to every record class rather than only the one
    whose scan remembered to ask.
    """
    if not directory.is_dir():
        return
    for p in sorted(directory.glob(pattern)):
        if is_syncable(p):
            yield p


def write_atomic(target: Path, body: bytes) -> None:
    """Publish ``body`` at ``target`` as a single visible step.

    The sync-side name for the repo-wide primitive in ``config._atomic`` — kept
    because every sync module already reaches for its disk verbs here. The
    guarantees (one whole body under concurrent writers, fsynced content, no
    temp litter, and *no* durability for the rename itself) are documented
    there; this is not a second implementation.
    """
    _atomic.write_bytes(target, body)


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
