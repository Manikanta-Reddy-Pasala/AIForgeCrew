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

# Registry caps. A mesh is a handful of operator-owned machines, so 64 rows is
# far past any real deployment and still small enough that every consumer of the
# registry (the admin page probes each row) stays cheap. MAX_NEW_PER_MERGE bounds
# how fast one hostile roster can consume the remaining space — a genuine new
# peer still lands on the first cycle it is gossiped, a roster of 200 does not.
MAX_PEERS = 64
MAX_NEW_PER_MERGE = 8


def as_epoch(value) -> int:
    """A ``last_seen`` stamp read tolerantly — peers.json is hand-editable.

    Lives here rather than in ``election`` because ``last_seen`` is a registry
    field: the reader and the writer must agree on what an unreadable one means.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def normalise_id(value) -> str:
    """A peer id in the canonical identity alphabet, or ``""`` if unusable.

    The same slug :func:`identity.self_id` produces — imported rather than
    restated, because the whole point is that the two can never disagree.
    Without it a hand-typed ``"NUC-Prod"`` and the machine's own ``"nuc-prod"``
    are two different peers: the registry grows a second row for one machine and
    the election elects an id that nobody answers to.

    An id that does not survive ``paths.is_addressable`` is refused rather than
    repaired, for the reason ``paths`` states: repairing invents an identity the
    originating peer never used, and two distinct ids can collapse onto one.
    """
    from aiforge_core.memory.sync import identity, paths

    raw = str(value or "").strip()
    if not raw:
        return ""
    slug = identity._slug(raw)   # the one slug function; see identity._slug
    return slug if paths.is_addressable(slug) else ""


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
    """Peers this node is willing to pull from. A bad row costs only itself.

    ``[p for p in ... if p.get("state")]`` raised on the first row that was not
    a dict, and peers.json is hand-editable and gossip-fed, so one stray string
    is entirely ordinary. The caller (``loop.run_once``) catches that as "the
    registry is unreadable" and returns no peers at all — so a single malformed
    row silently dropped *every healthy peer*, every cycle, forever, taking the
    compaction pass that rides the same loop with it. Skipping the row confines
    the damage to the row.
    """
    out, dropped = [], 0
    for p in load()["peers"]:
        if not isinstance(p, dict):
            dropped += 1
            continue
        try:
            if p.get("state") == STATE_APPROVED:
                out.append(p)
        except Exception:  # noqa: BLE001 — a row that cannot be read is not approved
            dropped += 1
    if dropped:
        # One line per cycle, not per row: a file full of junk must not become
        # a log full of junk.
        _log.warning("sync: %d unreadable row(s) in peers.json skipped", dropped)
    return out


def mesh_key() -> str:
    """The shared mesh secret (AIFORGE_MESH_KEY), or "".

    When set, three things change: it is the bearer every pull sends (replacing
    the per-peer, human-copied token); a candidate that answers it with a real
    manifest holds the same secret and therefore auto-joins; and — enforced in
    ``api._require_token``, not here — it unlocks ONLY the sync routes, never
    the control plane. When unset, approval stays a human step (a token copied
    out of band), i.e. the original, stricter model.
    """
    return (os.environ.get("AIFORGE_MESH_KEY") or "").strip()


def mesh_proof(nonce: str) -> str:
    """``HMAC-SHA256(mesh_key, nonce)`` hex, or "" when no mesh key is set.

    The one place the shared secret is turned into a proof, so the server that
    answers a challenge and the client that verifies one can never disagree on
    the construction. The key itself never leaves the process — only the HMAC,
    which is one-way, so answering a challenge does not reveal the key (a strong
    key also defeats offline brute-force over collected nonce/proof pairs).
    """
    import hashlib
    import hmac
    key = mesh_key()
    if not key or not nonce:
        return ""
    return hmac.new(key.encode(), nonce.encode(), hashlib.sha256).hexdigest()


def candidates() -> list[dict]:
    """Discovered-but-unapproved rows — the auto-join gate's input."""
    return [p for p in load()["peers"]
            if isinstance(p, dict) and p.get("state") == STATE_CANDIDATE]


def promote(peer_id: str, url: str = "") -> bool:
    """Flip a candidate to approved (shared-key auto-join). Idempotent.

    Returns True only on an actual state change, so the caller logs the join
    once. A ``url`` is adopted ONLY when the row has none — an approved peer's
    address is out-of-band configuration that a later gossip/discovery must
    never silently re-point (the same rule ``merge_roster`` enforces).
    """
    pid = normalise_id(peer_id)
    if not pid:
        return False
    data = load()
    changed = False
    for p in data["peers"]:
        if not isinstance(p, dict) or normalise_id(p.get("id")) != pid:
            continue
        if url and not (p.get("urls") or []):
            p["urls"] = [url]
        if p.get("state") != STATE_APPROVED:
            p["state"] = STATE_APPROVED
            changed = True
        break
    if changed:
        save(data)
    return changed


def roster() -> list[dict]:
    """What this node advertises to others. Ids and urls only — never tokens."""
    from aiforge_core.memory.sync.identity import self_id

    data = load()
    me = data["self"]
    out = [{"id": self_id(), "urls": list(me.get("urls") or []),
            "last_seen": int(time.time())}]
    for p in data["peers"]:
        # Canonical ids on the wire too: what we advertise is what the receiving
        # peer will compare against its own registry.
        pid = normalise_id(p.get("id"))
        if not pid:
            continue
        out.append({"id": pid, "urls": list(p.get("urls") or []),
                    "last_seen": as_epoch(p.get("last_seen"))})
    return out


def _index(data: dict) -> dict:
    """Existing rows keyed by canonical id, normalising ids on the way in.

    One machine may occupy exactly one row. A file that already holds both
    ``NUC-Prod`` and ``nuc-prod`` collapses to one — the approved row wins,
    since the other cannot be pulled from anyway — because two rows for one
    machine let the election pick an id no peer answers to.
    """
    index: dict[str, dict] = {}
    for p in data.get("peers") or []:
        if not isinstance(p, dict):
            # Same reason as ``approved``: one stray row in a hand-edited file
            # must not raise. This one is worse if it does — merge_roster and
            # touch both run *after* a peer's blobs are applied, so the peer's
            # last_seen never advances, it ages out of the election, and its
            # result row is lost even though the sync itself worked.
            _log.info("sync: dropping non-record peer row %r", p)
            continue
        pid = normalise_id(p.get("id"))
        if not pid:
            _log.info("sync: dropping unusable peer id %r", p.get("id"))
            continue
        p["id"] = pid                     # migrate the row in place
        cur = index.get(pid)
        if cur is None:
            index[pid] = p
        elif cur.get("state") != STATE_APPROVED and p.get("state") == STATE_APPROVED:
            _log.info("sync: collapsing duplicate rows for peer %s", pid)
            index[pid] = p
        else:
            _log.info("sync: collapsing duplicate rows for peer %s", pid)
    return index


def merge_roster(entries: list[dict]) -> dict:
    """Fold a peer's advertised roster into the local registry.

    Unknown peers are recorded as candidates. Nothing in a roster can modify a
    peer we already know: state, token, ``last_seen`` *and* ``urls`` arriving
    over the wire are dropped, so a compromised peer can add bounded noise but
    never mesh membership.

    ``urls`` are learned only when minting a new candidate. An approved peer's
    address is out-of-band configuration exactly like its token: adopting a
    gossiped url re-points that peer at whoever gossiped it, and the next cycle
    hands the attacker its bearer token — full access to the real peer's
    control plane — while they also become that peer for sync purposes.

    ``last_seen`` is dropped for a different reason. It means "when did *we*
    last reach this peer", stamped by :func:`touch` off our own clock, and the
    leader election subtracts it from our own ``now`` — so folding somebody
    else's clock reading into it would put a foreign clock back into the one
    calculation that exists to avoid comparing clocks across machines.
    """
    from aiforge_core.memory.sync.identity import self_id

    me = normalise_id(self_id())
    data = load()
    index = _index(data)
    added = 0

    for raw in entries or []:
        pid = normalise_id((raw or {}).get("id"))
        if not pid or pid == me:
            continue
        cur = index.get(pid)
        if cur is not None:
            urls = [str(u) for u in ((raw or {}).get("urls") or []) if u]
            if urls and urls != list(cur.get("urls") or []):
                # Log the divergence, keep the operator-configured address.
                _log.warning("sync: ignoring gossiped urls for known peer %s (%s)",
                             pid, urls)
            continue
        if len(index) >= MAX_PEERS:
            _log.warning("sync: peer registry at its cap of %d — refusing new peer %s",
                         MAX_PEERS, pid)
            break
        if added >= MAX_NEW_PER_MERGE:
            _log.warning("sync: %d new candidates this cycle — refusing the rest",
                         MAX_NEW_PER_MERGE)
            break
        index[pid] = {"id": pid,
                      "urls": [str(u) for u in ((raw or {}).get("urls") or []) if u],
                      "state": STATE_CANDIDATE,
                      "last_seen": 0}      # never contacted BY US yet
        added += 1
        _log.info("sync: discovered candidate peer %s", pid)

    data["peers"] = list(index.values())
    return save(data)


def touch(peer_id: str) -> None:
    """Record a successful contact so a peer ages out of the roster only when dead."""
    now = int(time.time())
    pid = normalise_id(peer_id)
    data = load()
    for p in data["peers"]:
        if not isinstance(p, dict):
            continue          # a junk row is not the peer we are stamping
        if pid and normalise_id(p.get("id")) == pid:
            p["last_seen"] = now
        elif as_epoch(p.get("last_seen")) > now:
            # A stamp AHEAD of our clock (bad RTC at boot, an NTP step, a
            # hand-edited file) never expires — `now - seen` is negative, so the
            # election keeps a dead peer alive for as long as the skew lasts.
            # Repaired here because this is the one place that writes the field.
            _log.info("sync: clamping future last_seen for peer %s", p.get("id"))
            p["last_seen"] = now
    save(data)


__all__ = ["load", "save", "approved", "roster", "merge_roster", "touch",
           "as_epoch", "normalise_id", "mesh_key", "mesh_proof", "candidates",
           "promote", "MAX_PEERS", "MAX_NEW_PER_MERGE", "STATE_APPROVED",
           "STATE_CANDIDATE"]
