"""Where an identity lives on disk. The single owner of the layout rule.

OKF ids are per-scope counters (``aiforge_core/memory/okf/store.py:127``), so
``(nuc, O-01)`` and ``(ms, O-01)`` are unrelated objects that both render to
``O-01.md``. A peer's advertised path is therefore a hint, never an instruction:
trusting it would let one peer silently overwrite another's node.

The rule: an identity already held is updated wherever it currently lives;
anything new from another peer lands under ``okf/peers/<origin>/``. Every peer
derives the same answer from the same inputs, so the layout converges along with
the content.
"""
from __future__ import annotations

import re
from pathlib import Path

from aiforge_core.memory.sync import _io, merge

LEASE_KEY = "__lease__"

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


def lease_path() -> Path:
    return okf_dir() / ".lease.json"


def peers_dir() -> Path:
    """Root of the foreign-node tree."""
    return okf_dir() / "peers"


def peer_node_path(origin: str, key: str) -> Path:
    return peers_dir() / _component(origin) / f"{_component(key)}.md"


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
    for p in _io.iter_syncable(okf_dir(), "**/*.md"):
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
    # and overwrite it. Only the lease may travel without an origin.
    if not is_addressable(key):
        return None
    if key == LEASE_KEY and not entry.get("tomb"):
        return lease_path()
    if not is_addressable(origin):
        return None

    if entry.get("tomb"):
        return tomb_path(origin, key)

    existing = node_paths(origin, key)
    return existing[0] if existing else peer_node_path(origin, key)


__all__ = ["sanitise", "is_addressable", "okf_dir", "tomb_dir", "tomb_path", "lease_path",
           "peers_dir", "peer_node_path", "node_paths",
           "target_for", "LEASE_KEY"]
