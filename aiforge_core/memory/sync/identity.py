"""This peer's identity, and the version stamp carried by mutable records.

Ordering uses a per-node counter rather than a timestamp. Peers include other
people's machines whose clocks disagree, sometimes badly; a clock-based
last-writer-wins would hand every conflict to the most wrong clock in the mesh.
"""
from __future__ import annotations

import os
import re
import socket


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", str(value or "")).strip("-").lower() or "peer"


def self_id() -> str:
    """Stable short slug naming this peer. ``AIFORGE_PEER_ID`` wins."""
    env = (os.environ.get("AIFORGE_PEER_ID") or "").strip()
    if env:
        return _slug(env)
    return _slug(socket.gethostname())


def stamp(meta: dict) -> dict:
    """Return ``meta`` with ``origin``/``rev``/``updated_by`` advanced for a local write.

    ``origin`` is set once, by whichever peer minted the node, and never changes
    hands afterwards — it is half of the node's identity.
    """
    out = dict(meta or {})
    me = self_id()
    out["origin"] = str(out.get("origin") or me)
    out["rev"] = int(out.get("rev") or 0) + 1
    out["updated_by"] = me
    return out


__all__ = ["self_id", "stamp"]
