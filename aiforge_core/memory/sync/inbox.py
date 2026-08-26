"""The admin side of a push: what it wants, and what it will accept.

Kept out of the route so the acceptance rules can be tested without HTTP, and
so there is one place to read when asking "what can a spoke put on the admin".

A spoke may write **only what it authored**:

* class B nodes and tombstones whose ``origin`` is the pushing spoke, enforced
  by ``apply._accept_class_b`` exactly as it was for a pull. This is what a
  spoke actually sends (``push._mine``);
* class A records — captures and briefs — are still *accepted* if one is
  offered, on the create-only terms ``apply`` has always enforced, but nothing
  in the product pushes them: every machine compacts its own.

Two things it may never write:

* anything stamped ``derived:`` — that marker is the admin's merge, and
  admitting it would let a spoke place text in ``mesh/``, which every other
  spoke pulls and folds into the view its agents read. The merge ignores derived
  inputs anyway (``okf.tiers._authored``), so nothing legitimate is lost by
  refusing them at the door;
* anything claiming the admin's own origin, refused by ``apply`` for the reason
  it always was — only this machine may author its own identity space.

**There is no authentication here** (``AIFORGE_SYNC_AUTH=0``, the default, is
the deployment this was built for): a spoke states its own id and is believed. That makes the
origin check a *consistency* rule rather than a security boundary — it stops a
misconfigured spoke from clobbering another's nodes, not a hostile one on the
same network. Bind the admin to a trusted interface; see ``docs`` and
``api._sync_open``.
"""
from __future__ import annotations

import logging

from aiforge_core.config.paths import config_dir
from aiforge_core.memory.sync import transport

_log = logging.getLogger("aiforge.sync")

# The same caps the pull side enforces, applied to what arrives instead of to
# what is fetched. An offer is a manifest and a push is a blob, so reusing the
# transport numbers keeps one answer to "how big may this be" per shape.
MAX_OFFER_ENTRIES = transport.MAX_MANIFEST_ENTRIES
MAX_BLOB_BYTES = transport.MAX_BLOB_BYTES


def _roll_path():
    """Where the admin notes which spokes have talked to it.

    Observability only — nothing reads it to decide anything. It is config-dir
    state rather than memory because it is a fact about this machine's
    deployment, not knowledge, and it must never sync.
    """
    from pathlib import Path

    d = Path(str(config_dir()))
    d.mkdir(parents=True, exist_ok=True)
    return d / "spokes.json"


# A hub is a handful of operator-owned machines. The cap exists because the id
# is self-asserted and unauthenticated: without it, anything that can reach the
# port could grow this file without bound, one made-up id at a time.
MAX_SPOKES = 64


def seen(peer_id: str) -> None:
    """Record that ``peer_id`` reached us just now. Never raises.

    Soft-fails silently: losing a line of bookkeeping must not fail a push that
    otherwise succeeded.
    """
    import time

    from aiforge_core.memory.sync import _io, paths

    slug = paths.fold(peer_id)
    if not slug or not paths.is_addressable(slug):
        return
    try:
        rec = _io.read_json(_roll_path())
        rows = {str(k): int(v) for k, v in (rec.get("spokes") or {}).items()
                if isinstance(k, str)}
        if slug not in rows and len(rows) >= MAX_SPOKES:
            _log.warning("sync: %d spokes already recorded (cap %d) — not "
                         "recording %s", len(rows), MAX_SPOKES, slug)
            return
        rows[slug] = int(time.time())
        _io.write_json(_roll_path(), {"spokes": rows})
    except Exception as exc:  # noqa: BLE001 — bookkeeping is not the payload
        _log.info("sync: could not record spoke %s: %s", slug, exc)


def roll() -> list[dict]:
    """Every spoke that has reached us, most recent first."""
    from aiforge_core.memory.sync import _io

    try:
        rows = (_io.read_json(_roll_path()).get("spokes") or {}).items()
        out = [{"id": str(k), "last_seen": int(v)} for k, v in rows]
    except Exception:  # noqa: BLE001 — an unreadable roll is an empty one
        return []
    return sorted(out, key=lambda r: -r["last_seen"])


def wanted(entries: list[dict]) -> list[dict]:
    """Which of a spoke's advertised entries this machine does not already hold.

    The same ``merge.plan_sync`` the pull side runs, with the roles swapped: the
    spoke plays the remote, we play the local. Returns the winning entries
    themselves rather than bare hashes so the pusher sends back exactly what was
    asked for and the acceptance check has the metadata it needs.
    """
    from aiforge_core.memory.sync import manifest, merge

    rows = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue        # a spoke may send anything; a non-record is not one
        row = dict(e)
        row["hash"] = str(row.get("hash") or "").strip().lower()
        rows.append(row)
    if len(rows) > MAX_OFFER_ENTRIES:
        _log.warning("sync: spoke offered %d entries (cap %d) — refused",
                     len(rows), MAX_OFFER_ENTRIES)
        return []
    return merge.plan_sync(manifest.build(), [r for r in rows if not r.get("derived")])["want"]


def accept(peer_id: str, entry: dict, body: bytes) -> bool:
    """Apply one pushed blob. False means it was refused.

    Every check ``apply.apply_blob`` makes for a pulled blob applies here
    unchanged — the hash agrees with the bytes, the body agrees with the
    advertised metadata, the origin is the peer that sent it, the target on disk
    is still older. The two rules added on top are the ones a push introduces:
    a size cap on bytes we did not ask for, and the ``derived`` refusal.
    """
    from aiforge_core.memory.sync import apply

    if not isinstance(entry, dict):
        return False
    if len(body) > MAX_BLOB_BYTES:
        _log.warning("sync: pushed blob %s is %d bytes (cap %d) — refused",
                     entry.get("path"), len(body), MAX_BLOB_BYTES)
        return False
    if str(entry.get("derived") or "").strip():
        _log.warning("sync: spoke %s pushed a derived node (%s) — refused",
                     peer_id or "<unattributed>", entry.get("path"))
        return False
    if (entry.get("kind") == "B" and not entry.get("tomb")
            and not _passes_filter(peer_id, entry, body)):
        # Tombstones are exempt for the reason ``push._permitted`` gives: a
        # deletion carries no knowledge, and refusing one strands the node it
        # was meant to remove.
        return False
    try:
        return apply.apply_blob(entry, body, peer_id=peer_id)
    except OSError as exc:
        # One unwritable record must not fail the whole push — the spoke
        # re-offers it next cycle and would fail identically forever otherwise.
        _log.warning("sync: could not apply pushed %s: %s", entry.get("path"), exc)
        return False


def _passes_filter(peer_id: str, entry: dict, body: bytes) -> bool:
    """Re-run the outbound filter on what a spoke pushed. Defence in depth.

    The spoke is supposed to have filtered this already (``push._permitted``),
    and a spoke on the current build always has. A spoke on an OLDER build has
    not, and the admin is where such a node stops being one machine's problem
    and becomes every machine's: the fold reads it, and what the fold produces
    is what every other spoke pulls into the view its agents read.

    The bytes are already in hand and every rule is a regex, so this costs
    nothing worth measuring.
    """
    from aiforge_core.memory.okf import nodes
    from aiforge_core.memory.sync import redact

    try:
        verdict = redact.review(nodes.parse_node(body.decode("utf-8")))
    except ValueError:          # UnicodeDecodeError is one of these
        verdict = redact.Verdict(False, "filter.unreadable",
                                 "the pushed body could not be parsed")
    if verdict.send:
        return True
    _log.warning("sync: refusing pushed node %s from %s: %s",
                 entry.get("key") or entry.get("path"),
                 peer_id or "<unattributed>", verdict.rule)
    return False


def downstream() -> list[dict]:
    """What this machine serves to spokes: what it merged, and nothing else.

    Two shapes, both minted here:

    * class B nodes carrying the mesh marker — the tier-1 merge across
      everybody's knowledge;
    * our own **tombstones**. A tombstone is JSON and carries no ``derived``
      marker, so a marker-only filter never advertised one — and then
      ``tiers._retire_own_mesh``, whose entire purpose is that "its tombstone
      propagates the removal instead of letting the next pull bounce the node
      back", could not propagate anything: move the admin from A to B and every
      spoke keeps ``mesh/<A>/`` on disk forever. Only ours travel, so this
      cannot become a way to relay somebody else's deletion.

    Nothing else travels down. ``captures/`` and ``compacted/`` are each
    machine's own raw text and its own briefs: every machine runs its own
    compaction (``md_store.compact``), so shipping one machine's briefs to
    another would duplicate work that has already been done and feed a foreign
    scope's text back into a local fold.

    Relay of another spoke's raw node is refused for the reason
    ``apply._accept_class_b`` gives: nothing signs an entry's origin, so a
    relayed record is indistinguishable from a forged one. Here it is also
    unnecessary — the merge is what the other spokes need.
    """
    from aiforge_core.memory.okf import tiers
    from aiforge_core.memory.sync import identity, manifest, paths

    me = paths.fold(identity.self_id())
    return [e for e in manifest.build()
            if paths.fold(str(e.get("origin") or "")) == me
            and (str(e.get("derived") or "") == tiers.MESH or e.get("tomb"))]


__all__ = ["wanted", "accept", "downstream", "seen", "roll",
           "MAX_OFFER_ENTRIES", "MAX_BLOB_BYTES", "MAX_SPOKES"]
