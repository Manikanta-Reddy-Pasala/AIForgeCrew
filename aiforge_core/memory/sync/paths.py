"""Where an identity lives on disk. The single owner of the layout rule.

OKF ids are per-scope counters (``aiforge_core/memory/okf/store.py:127``), so
``(nuc, O-01)`` and ``(ms, O-01)`` are unrelated objects that both render to
``O-01.md``. A peer's advertised path is therefore a hint, never an instruction:
trusting it would let one peer silently overwrite another's node.

The rule: an identity already held is updated wherever it currently lives;
anything new from another peer lands under ``peers/<origin>/``. Every peer
derives the same answer from the same inputs, so the layout converges along with
the content.

Four directories under the memory root, each with exactly one writer
(``docs/superpowers/specs/2026-07-20-two-tier-knowledge-compaction.md``):

* ``okf/``          — this machine's own authored knowledge; the only thing we
                      contribute to the mesh.
* ``peers/<origin>/`` — raw inbox written by the sync applier, never edited here.
* ``mesh/``         — the leader's global compaction result, received like any
                      other record.
* ``view/``         — the local tier-2 working view. Local-only by construction:
                      it is never advertised, so it can never travel.
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from aiforge_core.memory.sync import _io, merge

_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")


def sanitise(value: str, fallback: str = "_") -> str:
    """Reduce ``value`` to the identity alphabet: ``[A-Za-z0-9_-]``.

    The single owner of that alphabet, shared with ``identity.self_id`` — a peer
    id becomes the ``origin`` half of every node it mints, so a slug that this
    function would rewrite is an identity ``is_addressable`` then refuses.

    ``origin`` and ``key`` arrive from a peer's frontmatter, so they are
    attacker-controlled. Dots are stripped along with separators, which is what
    makes ``".."`` collapse to the fallback rather than climbing the tree.
    """
    return _UNSAFE.sub("-", str(value or "")).strip("-") or fallback


def _component(value: str) -> str:
    """One untrusted path component."""
    return sanitise(value)


def is_addressable(value: str) -> bool:
    """True when ``value`` survives sanitisation unchanged.

    A component that does not round-trip cannot be used as an identity: either
    it carries separator or glob metacharacters (``*``, ``[0-9]``, ``..``) and
    would address some other peer's node, or it collapses — every non-ASCII key
    sanitises to the same ``_`` — so distinct identities would share one file.
    Such records are refused entry to the identity space rather than repaired,
    because repairing them invents an identity the originating peer never used.
    """
    return bool(value) and _component(value) == value


def okf_dir() -> Path:
    return _io.root() / "okf"


def tomb_dir() -> Path:
    """Root of the tombstone tree. Callers enumerating tombstones start here
    rather than rebuilding the ``.tomb`` literal themselves."""
    return okf_dir() / ".tomb"


def tomb_path(origin: str, key: str) -> Path:
    return tomb_dir() / _component(origin) / f"{_component(key)}.json"


def peers_root() -> Path:
    """Root of the foreign-node inbox, a sibling of ``okf/``.

    Deliberately *outside* ``okf/``: compaction reads ``okf/`` as "my knowledge",
    so foreign raw nodes living there made every peer distil a different pile.
    """
    return _io.root() / "peers"


def legacy_peers_dir() -> Path:
    """Where foreign nodes landed before the split. Only the migration reads it —
    it exists here so no other module has to spell the old layout out."""
    return okf_dir() / "peers"


def mesh_dir() -> Path:
    """The leader's tier-1 compaction result. Received, never authored locally
    (except on the leader itself)."""
    return _io.root() / "mesh"


def view_dir() -> Path:
    """The local tier-2 working view. Regenerated, never synced."""
    return _io.root() / "view"


def node_roots() -> tuple[Path, ...]:
    """Every directory that may hold a class B node file.

    ``view/`` is absent on purpose — see ``manifest._class_b``. The manifest
    builder and ``node_paths`` share this list so the file we advertise is the
    file we compare and write (the one-winner rule, I1).
    """
    return (okf_dir(), peers_root(), mesh_dir())


def node_scans() -> tuple[tuple[Path, str], ...]:
    """``(directory, glob)`` for every node root, for callers that stat rather
    than read (the manifest fingerprint)."""
    return tuple((d, "**/*.md") for d in node_roots())


def iter_nodes() -> Iterator[Path]:
    """Every real node file across the node roots, in directory then path order."""
    for directory, pattern in node_scans():
        yield from _io.iter_syncable(directory, pattern)


def peer_node_path(origin: str, key: str) -> Path:
    return peers_root() / _component(origin) / f"{_component(key)}.md"


def node_paths(origin: str, key: str) -> list[Path]:
    """Every node file on disk carrying this identity, highest ``rev`` first.

    ``key`` arrives from a peer's manifest, so the filename is compared
    literally rather than handed to ``rglob`` as a pattern: a peer advertising
    ``key="*"`` must not resolve to somebody else's node.

    The order is the layout half of the one-winner rule (I1): when the same
    identity is held in two scopes, ``[0]`` is the version the manifest also
    advertises, so the file we compare is the file we write. Ties on ``rev``
    break on the path, which every peer computes identically.
    """
    if not is_addressable(key):
        # Sanitising would silently retarget: "**/L-07" collapses onto L-07.md.
        return []
    name = f"{key}.md"
    ranked: list[tuple[int, str, Path]] = []
    # A ``.conflict.md`` sidecar can never match: it is `<stem>.conflict.md`,
    # and a key containing a dot does not survive _component().
    for p in iter_nodes():
        if p.name != name:
            continue
        meta = _io.read_node_meta(p)
        if not meta:      # unreadable or frontmatter-less — left untouched
            continue
        if str(meta.get("origin") or "") == origin:
            ranked.append((merge.as_rev(meta.get("rev")), p.as_posix(), p))
    ranked.sort(key=lambda t: (-t[0], t[1]))
    return [t[2] for t in ranked]


def target_for(entry: dict) -> Path | None:
    """Local destination for a manifest entry, or None if it must be refused."""
    if entry.get("kind") == "A":
        # Capture filenames embed a content digest, so they are globally unique.
        return _io.safe_target(str(entry.get("path") or ""))

    key = str(entry.get("key") or "")
    origin = str(entry.get("origin") or "")

    # The entry came from a peer and was never seen by our manifest builder, so
    # the identity rule is enforced here too: a key that does not round-trip
    # sanitisation ("*", "**/L-07") would sanitise onto an unrelated local node
    # and overwrite it.
    if not is_addressable(key):
        return None
    if not is_addressable(origin):
        return None

    if entry.get("tomb"):
        return tomb_path(origin, key)

    existing = node_paths(origin, key)
    return existing[0] if existing else peer_node_path(origin, key)


__all__ = ["sanitise", "is_addressable", "okf_dir", "tomb_dir", "tomb_path",
           "peers_root", "legacy_peers_dir", "mesh_dir", "view_dir",
           "node_roots", "node_scans", "iter_nodes", "peer_node_path",
           "node_paths", "target_for"]
