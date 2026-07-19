"""Peer registry — ``$AIFORGE_CONFIG_DIR/peers.json``.

This is local configuration, not memory: it is never synced and never appears
in the manifest. The gossiped roster is merged *into* it, but discovery is not
trust — a learned peer lands in ``candidate`` state, is never pulled from, and
is promoted only when a human supplies a token obtained out of band.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from aiforge_core.memory.sync import _io

_log = logging.getLogger("aiforge.sync")

STATE_APPROVED = "approved"
STATE_CANDIDATE = "candidate"


def _path() -> Path:
    # peers.json is CONFIG, not memory — it lives beside the other config files
    # and is never synced, so it does not go under the memory tree.
    d = Path(os.path.expanduser(os.environ.get("AIFORGE_CONFIG_DIR", "~/.aiforge")))
    d.mkdir(parents=True, exist_ok=True)
    return d / "peers.json"


def load() -> dict:
    data = _io.read_json(_path())
    return {"self": data.get("self") or {}, "peers": data.get("peers") or []}


def save(data: dict) -> dict:
    _io.write_json(_path(), data)
    return data


def approved() -> list[dict]:
    """Peers this node is willing to pull from."""
    return [p for p in load()["peers"] if p.get("state") == STATE_APPROVED]


def roster() -> list[dict]:
    """What this node advertises to others. Ids and urls only — never tokens."""
    from aiforge_core.memory.sync.identity import self_id

    data = load()
    me = data["self"]
    out = [{"id": self_id(), "urls": list(me.get("urls") or []),
            "last_seen": int(time.time())}]
    for p in data["peers"]:
        out.append({"id": p.get("id"), "urls": list(p.get("urls") or []),
                    "last_seen": int(p.get("last_seen") or 0)})
    return out


def merge_roster(entries: list[dict]) -> dict:
    """Fold a peer's advertised roster into the local registry.

    Unknown peers are recorded as candidates. Nothing in a roster can promote a
    peer or grant a token: state and token fields arriving over the wire are
    dropped, so a compromised peer can add noise but never mesh membership.
    """
    from aiforge_core.memory.sync.identity import self_id

    me = self_id()
    data = load()
    index = {p.get("id"): p for p in data["peers"]}

    for raw in entries or []:
        pid = str((raw or {}).get("id") or "").strip()
        if not pid or pid == me:
            continue
        urls = [str(u) for u in (raw.get("urls") or []) if u]
        seen = int(raw.get("last_seen") or 0)
        cur = index.get(pid)
        if cur is None:
            index[pid] = {"id": pid, "urls": urls, "state": STATE_CANDIDATE,
                          "last_seen": seen}
            _log.info("sync: discovered candidate peer %s", pid)
            continue
        if urls:
            cur["urls"] = urls
        cur["last_seen"] = max(int(cur.get("last_seen") or 0), seen)

    data["peers"] = list(index.values())
    return save(data)


def touch(peer_id: str) -> None:
    """Record a successful contact so a peer ages out of the roster only when dead."""
    data = load()
    for p in data["peers"]:
        if p.get("id") == peer_id:
            p["last_seen"] = int(time.time())
    save(data)


__all__ = ["load", "save", "approved", "roster", "merge_roster", "touch",
           "STATE_APPROVED", "STATE_CANDIDATE"]
