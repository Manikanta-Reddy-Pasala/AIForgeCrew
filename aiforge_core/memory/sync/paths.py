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

from aiforge_core.memory.sync import _io

LEASE_KEY = "__lease__"

_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")


def _component(value: str) -> str:
    """Sanitise one untrusted path component.

    ``origin`` and ``key`` arrive from a peer's frontmatter, so they are
    attacker-controlled. Dots are stripped along with separators, which is what
    makes ``".."`` collapse to the empty string rather than climbing the tree.
    """
    return _UNSAFE.sub("-", str(value or "")).strip("-") or "_"


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
    """Every node file on disk carrying this identity, across all scopes."""
    from aiforge_core.memory.okf import nodes as _nodes

    okf = okf_dir()
    if not okf.is_dir():
        return []
    out: list[Path] = []
    for p in sorted(okf.rglob(f"{key}.md")):
        if p.name.endswith(".conflict.md"):
            continue
        try:
            meta = (_nodes.parse_node(p.read_text(encoding="utf-8")).get("meta") or {})
        except Exception:  # noqa: BLE001 — an unreadable node is left untouched
            continue
        if str(meta.get("origin") or "") == origin:
            out.append(p)
    return out


def target_for(entry: dict) -> Path | None:
    """Local destination for a manifest entry, or None if it must be refused."""
    if entry.get("cls") == "A":
        # Capture filenames embed a content digest, so they are globally unique.
        return _io.safe_target(str(entry.get("path") or ""))

    key = str(entry.get("key") or "")
    if not key:
        return None
    origin = str(entry.get("origin") or "")

    if entry.get("tomb"):
        return tomb_path(origin, key)
    if key == LEASE_KEY:
        return lease_path()

    existing = node_paths(origin, key)
    return existing[0] if existing else peer_node_path(origin, key)


__all__ = ["okf_dir", "tomb_dir", "tomb_path", "lease_path", "peers_dir",
           "peer_node_path", "node_paths",
           "target_for", "LEASE_KEY"]
