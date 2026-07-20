"""Placing a fetched blob into the local tree. Knows nothing about HTTP.

Every blob is verified against the hash its peer advertised before it touches
the tree, and every write goes through ``_io.write_atomic``, so an interrupted
or tampered fetch can never leave a partial or forged note behind. A rejected
blob is simply dropped — it reappears in the next diff.

Three things are checked here that the plan cannot check for us, because the
plan is a snapshot of two manifests and the write happens a network round-trip
later:

* the fetched **body agrees with the advertised metadata**, so the ``rev`` that
  won the merge is the ``rev`` actually being written;
* the entry does not claim **our own origin**, which nobody else may speak for;
* the **target on disk is still older**, re-read immediately before the write,
  so an edit made during the cycle is not silently destroyed.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from aiforge_core.memory.sync import _io, identity, manifest, merge, paths

_log = logging.getLogger("aiforge.sync")


def _digest(value) -> str:
    """A hash in the one form we compare in. The manifest emits lowercase hex;
    a peer emitting uppercase otherwise conflicts with us forever, because the
    two spellings never match and the entry can therefore never be applied."""
    return str(value or "").strip().lower()


def _ident(fields: dict) -> tuple:
    """Case-folded identity + version of a class B record, for comparison only."""
    return (paths.fold(fields["origin"]), paths.fold(fields["key"]),
            fields["rev"], paths.fold(fields["updated_by"]))


def _body_fields(entry: dict, body: bytes) -> dict | None:
    """Identity fields as stated by the fetched bytes, or None if unreadable.

    A tombstone travels as JSON and a node as frontmatter; both carry the same
    four fields, so both are read back into the same shape.
    """
    from aiforge_core.memory.okf import nodes as _nodes

    try:
        text = body.decode("utf-8")
        meta = json.loads(text) if entry.get("tomb") else _nodes.parse_node(text).get("meta")
    except Exception:  # noqa: BLE001 — a peer's bytes must not raise, only be refused
        return None
    if not isinstance(meta, dict):
        return None
    return manifest.fields_of(meta)


def _order_on_disk(target: Path, *, tomb: bool) -> tuple | None:
    """``merge``'s total order for whatever currently occupies ``target``.

    None when there is nothing to compare — no file, or a file with no
    frontmatter at all, which is not a version of anything.
    """
    if not target.is_file():
        return None
    try:
        meta = _io.read_json(target) if tomb else _io.read_node_meta(target)
        if not meta:
            return None
        fields = manifest.fields_of(meta)
        return (fields["rev"], fields["updated_by"], _io.sha256_file(target))
    except OSError:  # unreadable right now: treat as "nothing to compare"
        return None


def apply_blob(entry: dict, body: bytes) -> bool:
    """Verify and write one fetched blob. False means it was rejected."""
    if hashlib.sha256(body).hexdigest() != _digest(entry.get("hash")):
        _log.warning("sync: hash mismatch for %s, dropping", entry.get("path"))
        return False

    if entry.get("kind") == "B" and not _accept_class_b(entry, body):
        return False

    target = paths.target_for(entry)
    if target is None:
        return False

    if entry.get("kind") == "B" and not _target_is_older(entry, target, body):
        return False

    _io.write_atomic(target, body)
    _enforce_invariant(entry)
    return True


def _accept_class_b(entry: dict, body: bytes) -> bool:
    """Refuse an entry that lies about itself, or that speaks for our origin."""
    declared = manifest.fields_of(entry)

    if paths.fold(declared["origin"]) == identity.self_id():
        # Only this machine may author its own origin. A peer sending one
        # rewrites nodes it never authored — including a tombstone at an
        # arbitrary rev, which deletes ours everywhere. The real fix is signed
        # manifests, so a peer cannot claim any origin but its own; that is out
        # of scope here, and this refusal is the half of it we can enforce
        # locally without a key exchange.
        _log.warning("sync: peer entry %s claims our own origin %s, refusing",
                     entry.get("path"), declared["origin"])
        return False

    actual = _body_fields(entry, body)
    if actual is None or _ident(actual) != _ident(declared):
        # The manifest decided the merge; the body is what gets written. A peer
        # advertising rev 999 over a body saying rev 1 otherwise overwrote a
        # newer local node with older content — stably, every cycle.
        _log.warning("sync: %s body disagrees with its manifest entry, refusing",
                     entry.get("path"))
        return False
    return True


def _target_is_older(entry: dict, target: Path, body: bytes) -> bool:
    """True when it is safe to overwrite ``target``.

    The plan was computed from a manifest snapshot taken before the fetch, so
    the target is re-read here rather than trusted: a local edit landing inside
    that window was otherwise destroyed with no sidecar and no log. A remote
    that loses this comparison is kept beside the node rather than dropped.
    """
    current = _order_on_disk(target, tomb=bool(entry.get("tomb")))
    if current is None:
        return True
    incoming = (merge.as_rev(entry.get("rev")),
                str(entry.get("updated_by") or entry.get("origin") or ""),
                _digest(entry.get("hash")))
    if current[2] == incoming[2]:      # identical bytes already in place
        return False
    if current < incoming:
        return True
    _log.warning("sync: %s is stale against the local copy, keeping ours",
                 entry.get("path"))
    if not entry.get("tomb"):
        # A losing tombstone carries no authored text, so there is nothing a
        # sidecar could preserve — only a node's body is worth keeping.
        keep_conflict({"path": _io.rel(target), "key": entry.get("key")}, body)
    return False


def _enforce_invariant(entry: dict) -> None:
    """For one (origin, key), either the node file or its tombstone exists, never both."""
    if entry.get("kind") != "B":
        return
    key = str(entry.get("key") or "")
    if not key:
        return
    origin = str(entry.get("origin") or "")
    if entry.get("tomb"):
        for p in paths.node_paths(origin, key):
            p.unlink(missing_ok=True)
    else:
        paths.tomb_path(origin, key).unlink(missing_ok=True)


def keep_conflict(local_entry: dict, remote_body: bytes | None = None) -> Path | None:
    """Preserve the *losing* version beside the node as a ``.conflict`` sidecar.

    Which version that is depends on who won. When the remote wins, the local
    file is about to be overwritten and is what needs preserving, so pass no
    body. When the local copy wins, sidecarring it would file a byte-identical
    duplicate of the live node every cycle while the remote's text — the only
    copy about to be lost — is preserved nowhere; pass ``remote_body`` and that
    is what is kept.

    Sidecars are local artefacts, excluded from the manifest: replicating them
    would multiply one collision across the whole mesh.
    """
    target = _io.safe_target(str(local_entry.get("path") or ""))
    if target is None or not target.is_file():
        return None
    sidecar = target.with_name(target.stem + ".conflict.md")
    try:
        _io.write_atomic(sidecar, target.read_bytes() if remote_body is None
                         else remote_body)
    except OSError:  # losing the sidecar must not abort the sync
        _log.warning("sync: could not write conflict sidecar for %s", target)
        return None
    _log.info("sync: conflict on %s, kept losing version at %s",
              local_entry.get("key"), sidecar.name)
    return sidecar


__all__ = ["apply_blob", "keep_conflict"]
