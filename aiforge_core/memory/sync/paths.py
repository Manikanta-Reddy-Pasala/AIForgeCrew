"""Where an identity lives on disk. The single owner of the layout rule.

OKF ids are per-scope counters (``aiforge_core/memory/okf/store.py:127``), so
``(nuc, O-01)`` and ``(ms, O-01)`` are unrelated objects that both render to
``O-01.md``. A peer's advertised path is therefore a hint, never an instruction:
trusting it would let one peer silently overwrite another's node.

The rule: an identity already held is updated wherever it currently lives —
outside ``okf/``, which only this machine writes (see ``_is_ours``); a new
node marked ``derived: mesh`` is the leader's fold and lands in ``mesh/``;
anything else new from another peer lands under ``peers/<origin>/``. Every peer
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

# Longest path component we will address. Below every filesystem's NAME_MAX
# (255 on ext4/APFS, and a `.tmp` staging prefix is added on top), so a hostile
# key is refused at validation rather than surfacing as OSError from open().
_MAX_COMPONENT = 128

# The two class A directories, as scanned by ``manifest._class_a``. Named here
# because this module owns the layout: the scan and the write rule must agree,
# or a peer can address a directory nothing ever advertises.
CLASS_A_DIRS = ("captures", "compacted")


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


def fold(value: str) -> str:
    """Identity-comparison form of one component: sanitised, then lowercased.

    ``identity._slug`` already lowercases every peer id it mints, so folding is
    what makes the comparison agree with the name on disk. Without it a peer
    claiming ``origin: "MS"`` misses the ``peers/ms/`` node it is actually
    addressing — on a case-insensitive filesystem it then writes the same file
    while the merge believed it was a brand new identity.
    """
    return _component(value).lower()


def is_addressable(value: str) -> bool:
    """True when ``value`` survives sanitisation unchanged and fits a filename.

    A component that does not round-trip cannot be used as an identity: either
    it carries separator or glob metacharacters (``*``, ``[0-9]``, ``..``) and
    would address some other peer's node, or it collapses — every non-ASCII key
    sanitises to the same ``_`` — so distinct identities would share one file.
    Such records are refused entry to the identity space rather than repaired,
    because repairing them invents an identity the originating peer never used.

    The length cap is part of the same rule: a 400-character key is a valid
    identity string that no filesystem can hold, and refusing it here is what
    keeps the failure a rejected record rather than an OSError raised from the
    middle of a cycle.
    """
    return bool(value) and len(value) <= _MAX_COMPONENT and _component(value) == value


def class_a_dirs() -> tuple[Path, ...]:
    """The directories immutable content-addressed records live in."""
    return tuple(_io.root() / d for d in CLASS_A_DIRS)


def class_a_scans() -> tuple[tuple[Path, str], ...]:
    """``(directory, glob)`` for class A. Flat: a capture filename embeds its
    own digest, so the class has no subtree and needs none."""
    return tuple((d, "*.md") for d in class_a_dirs())


def class_a_target(relative: str) -> Path | None:
    """Local destination for a class A entry, or None if it must be refused.

    The path is peer-supplied, so it is constrained to exactly what
    ``manifest._class_a`` scans: ``<captures|compacted>/<name>.md``, one
    component, no dotfiles. Trusting it verbatim let a peer write anywhere in
    the tree that ``_io.safe_target`` calls "inside" — over our own ``okf/``
    notes, into ``view/`` (which must never receive remote data), or into
    ``okf/.tomb/`` as a forged tombstone we would then re-advertise ourselves.
    ``safe_target`` stays underneath as the traversal guard.
    """
    parts = str(relative or "").split("/")
    if len(parts) != 2 or parts[0] not in CLASS_A_DIRS:
        return None
    name = parts[1]
    if not name.endswith(".md") or not is_addressable(name[:-3]):
        # is_addressable refuses "", "..", dotfiles and anything with a
        # separator — the same alphabet that guards a class B identity.
        return None
    return _io.safe_target(f"{parts[0]}/{name}")


def okf_dir() -> Path:
    return _io.root() / "okf"


def tomb_dir() -> Path:
    """Root of the tombstone tree. Callers enumerating tombstones start here
    rather than rebuilding the ``.tomb`` literal themselves."""
    return okf_dir() / ".tomb"


def tomb_path(origin: str, key: str) -> Path:
    return tomb_dir() / fold(origin) / f"{_component(key)}.json"


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
    return peers_root() / fold(origin) / f"{_component(key)}.md"


def mesh_node_path(origin: str, key: str) -> Path:
    """Where an arriving tier-1 result lands: ``mesh/<origin>/<key>.md``.

    Keyed on the origin as well as the key, mirroring the peer inbox. Flat, the
    two folds a partition produces are one filename — so healing the partition
    overwrote one leader's fold with the other's, silently and with no conflict
    reported (they never shared an identity key), and one peer tombstoning "its"
    node wiped the mesh for everybody. Two folds are two identities.
    """
    return mesh_dir() / fold(origin) / f"{_component(key)}.md"


def _mesh_marker() -> str:
    """The ``derived:`` value tier 1 stamps on its output.

    Imported here rather than restated: the compaction tier owns the frontmatter
    vocabulary it writes, and a second copy of the literal is how the two drift
    apart. Lazy because ``okf.tiers`` reads its directories from this module.
    """
    from aiforge_core.memory.okf import tiers

    return tiers.MESH


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
    name = fold(key) + ".md"
    want_origin = fold(origin)
    ranked: list[tuple[int, str, Path]] = []
    # A ``.conflict.md`` sidecar can never match: it is `<stem>.conflict.md`,
    # and a key containing a dot does not survive _component().
    for p in iter_nodes():
        # Case-folded on both halves: an identity is one object however a peer
        # capitalises it, and on a case-insensitive filesystem "K-01" and "k-01"
        # are the same file — comparing literally called that a new identity and
        # wrote it anyway, bypassing the merge order entirely.
        if p.name.lower() != name:
            continue
        meta = _io.read_node_meta(p)
        if not meta:      # unreadable or frontmatter-less — left untouched
            continue
        if fold(str(meta.get("origin") or "")) == want_origin:
            ranked.append((merge.as_rev(meta.get("rev")), p.as_posix(), p))
    ranked.sort(key=lambda t: (-t[0], t[1]))
    return [t[2] for t in ranked]


def _is_ours(path: Path) -> bool:
    """True when ``path`` sits in ``okf/`` — this machine's authored space.

    ``okf/`` has exactly one writer, us. ``target_for`` is only ever asked about
    an entry that arrived from a peer, so an ``okf/`` answer is always wrong: a
    foreign-origin node that happens to sit there (a hand-moved file, a tree
    written by a build predating the okf//peers split) made "update the identity
    wherever it currently lives" mean *inside our own authored knowledge*, and a
    peer could then place its text in the one directory that is trusted as ours
    — compaction reads okf/ as "my knowledge" and folds it into the mesh.
    """
    okf = okf_dir()
    return path == okf or okf in path.parents


def target_for(entry: dict) -> Path | None:
    """Local destination for a manifest entry, or None if it must be refused."""
    if entry.get("kind") == "A":
        # Capture filenames embed a content digest, so they are globally unique
        # — but only inside the two directories the class is scanned from.
        return class_a_target(str(entry.get("path") or ""))

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

    existing = [p for p in node_paths(origin, key) if not _is_ours(p)]
    if existing:
        # An identity already held is updated where it lives, whichever
        # directory that is *except* okf/ (see ``_is_ours``): the file we
        # compare must be the file we write (I1). Relocating it here would
        # compare one path and write another forever.
        return existing[0]
    if str(entry.get("derived") or "") == _mesh_marker():
        # The leader's fold, arriving for the first time. Without this it would
        # land in the raw inbox, leaving mesh/ permanently empty on followers.
        return mesh_node_path(origin, key)
    return peer_node_path(origin, key)


__all__ = ["CLASS_A_DIRS", "sanitise", "fold", "is_addressable",
           "class_a_dirs", "class_a_scans", "class_a_target",
           "okf_dir", "tomb_dir", "tomb_path",
           "peers_root", "legacy_peers_dir", "mesh_dir", "view_dir",
           "node_roots", "node_scans", "iter_nodes", "peer_node_path",
           "mesh_node_path", "node_paths", "target_for"]
