"""What a peer may and may not make this node do.

Everything in a manifest is attacker-controlled: a peer is a LAN machine we
have approved, not a machine we trust. Each test here is a demonstrated attack
or a demonstrated stuck state, not a hypothetical.
"""
from __future__ import annotations

import hashlib


def _env(monkeypatch, tmp_path, peer_id: str = "book"):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", peer_id)


def _node_text(key: str, origin: str, rev: int, updated_by: str, body: str = "b") -> bytes:
    return (f'---\ntype: learning\nid: "{key}"\norigin: "{origin}"\nrev: {rev}\n'
            f'updated_by: "{updated_by}"\n---\n\n{body}\n').encode()


def _write(tmp_path, relative: str, body: bytes):
    p = tmp_path / "md" / relative
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)
    return p


def _entry(body: bytes, **fields) -> dict:
    return {"hash": hashlib.sha256(body).hexdigest(), **fields}


def _stub_transport(monkeypatch, entries, blobs):
    """A peer that advertises ``entries`` and serves ``blobs`` by hash."""
    from aiforge_core.memory.sync import transport

    monkeypatch.setattr(transport, "fetch_manifest",
                        lambda *a, **k: {"manifest": entries, "roster": []})
    monkeypatch.setattr(transport, "fetch_blob",
                        lambda base, digest, token="": blobs.get(str(digest).lower()))


def _sync(monkeypatch, entries, blobs) -> dict:
    from aiforge_core.memory.sync import loop

    _stub_transport(monkeypatch, entries, blobs)
    return loop.sync_with({"id": "nuc", "urls": ["http://stub"], "token": ""})


# ── 1. class A must not escape its two directories ────────────────────────

def test_class_a_cannot_write_outside_the_capture_dirs(monkeypatch, tmp_path):
    """A class A path was used verbatim, so "inside the tree" was the only rule:
    a peer overwrote our own okf/ note, wrote into view/ (local-only by
    construction) and dropped a dotfile at the root."""
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import apply

    mine = _write(tmp_path, "okf/global/learnings/O-01.md",
                  _node_text("O-01", "book", 5, "book", "mine"))
    body = b"pwn"

    for path in ("okf/global/learnings/O-01.md", "view/V-01.md", ".tiers.json",
                 "peers/ms/K-01.md", "captures/sub/dir.md", "captures/.hidden.md"):
        assert apply.apply_blob(_entry(body, kind="A", path=path), body) is False

    assert b"mine" in mine.read_bytes()
    assert not (tmp_path / "md" / "view").exists()
    assert not (tmp_path / "md" / ".tiers.json").exists()
    # ...and the legitimate destinations still work.
    assert apply.apply_blob(_entry(body, kind="A", path="captures/a.md"), body) is True
    assert apply.apply_blob(_entry(body, kind="A", path="compacted/c.md"), body) is True


def test_class_a_cannot_forge_a_tombstone(monkeypatch, tmp_path):
    """A forged tombstone is worse than a forged note: we re-advertise it as our
    own, so one peer's write deletes the node across the whole mesh."""
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import apply, manifest

    node = _write(tmp_path, "peers/alpha/O-02.md",
                  _node_text("O-02", "alpha", 2, "alpha", "real"))
    body = b'{"origin":"alpha","key":"O-02","rev":99,"updated_by":"x","tomb":true}'

    assert apply.apply_blob(
        _entry(body, kind="A", path="okf/.tomb/alpha/O-02.json"), body) is False
    assert node.is_file()
    assert not (tmp_path / "md" / "okf" / ".tomb").exists()
    assert not [e for e in manifest.build() if e.get("tomb")]


# ── 2. nobody else may speak for our own origin ───────────────────────────

def test_an_entry_claiming_our_own_origin_is_refused(monkeypatch, tmp_path):
    """A peer sent class B entries stamped with our id and rewrote a node it
    never authored, then deleted another with a tombstone at rev 500."""
    _env(monkeypatch, tmp_path, peer_id="book")
    from aiforge_core.memory.sync import apply

    mine = _write(tmp_path, "okf/global/learnings/L-07.md",
                  _node_text("L-07", "book", 5, "book", "mine"))
    doomed = _write(tmp_path, "okf/global/learnings/L-08.md",
                    _node_text("L-08", "book", 1, "book", "keep me"))

    forged = _node_text("L-07", "book", 99, "book", "theirs")
    assert apply.apply_blob(_entry(forged, kind="B", path="peers/book/L-07.md",
                                   origin="book", key="L-07", rev=99,
                                   updated_by="book"), forged) is False
    assert b"mine" in mine.read_bytes()

    tomb = b'{"origin":"book","key":"L-08","rev":500,"updated_by":"book","tomb":true}'
    assert apply.apply_blob(_entry(tomb, kind="B", path="okf/.tomb/book/L-08.json",
                                   origin="book", key="L-08", rev=500,
                                   updated_by="book", tomb=True), tomb) is False
    assert doomed.is_file()


# ── 3. the fetched body, and the target, are re-checked at write time ─────

def test_a_local_edit_during_the_cycle_is_not_destroyed(monkeypatch, tmp_path):
    """The plan is a snapshot; the write happens a round-trip later. An edit
    landing in that window was overwritten with no sidecar and no rejection."""
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import apply

    node = _write(tmp_path, "peers/nuc/L-07.md",
                  _node_text("L-07", "nuc", 99, "zeta", "edited mid-cycle"))
    body = _node_text("L-07", "nuc", 48, "nuc", "remote")

    assert apply.apply_blob(_entry(body, kind="B", path="peers/nuc/L-07.md",
                                   origin="nuc", key="L-07", rev=48,
                                   updated_by="nuc"), body) is False
    assert b"edited mid-cycle" in node.read_bytes()
    # the loser is preserved rather than dropped
    assert b"remote" in (node.parent / "L-07.conflict.md").read_bytes()


def test_a_body_disagreeing_with_its_entry_is_refused(monkeypatch, tmp_path):
    """The entry decides the merge but the body is what lands: advertising
    rev 999 over a body saying rev 1 overwrote a newer node with older text."""
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import apply

    node = _write(tmp_path, "peers/nuc/L-07.md",
                  _node_text("L-07", "nuc", 5, "nuc", "newer local"))
    body = _node_text("L-07", "nuc", 1, "nuc", "stale remote")

    assert apply.apply_blob(_entry(body, kind="B", path="peers/nuc/L-07.md",
                                   origin="nuc", key="L-07", rev=999,
                                   updated_by="nuc"), body) is False
    assert b"newer local" in node.read_bytes()


# ── 4. one bad entry must not end the cycle ───────────────────────────────

def test_an_overlong_key_is_refused_at_validation(monkeypatch, tmp_path):
    """`key = "A"*400` is a valid identity string no filesystem can hold. It
    reached open() and raised OSError from the middle of the cycle."""
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import paths

    key = "A" * 400

    assert paths.is_addressable(key) is False
    assert paths.target_for({"kind": "B", "origin": "nuc", "key": key,
                             "path": "x"}) is None


def test_one_unwritable_entry_does_not_abort_the_cycle(monkeypatch, tmp_path):
    """An OSError from one write discarded every later entry and skipped
    peers.touch, so the peer's last_seen froze and it left the election — and
    the entry is re-advertised every cycle, so it never synced again."""
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import apply, peers

    peers.save({"self": {"id": "book", "urls": []},
                "peers": [{"id": "nuc", "urls": ["http://stub"], "state": "approved"}]})

    good = b"good capture"
    bad = b"bad capture"
    entries = [_entry(bad, kind="A", path="captures/bad.md"),
               _entry(good, kind="A", path="captures/good.md")]
    blobs = {e["hash"]: b for e, b in zip(entries, (bad, good), strict=True)}

    real = apply.apply_blob

    def _boom(entry, body):
        if body == bad:
            raise OSError(63, "File name too long")
        return real(entry, body)

    monkeypatch.setattr(apply, "apply_blob", _boom)
    res = _sync(monkeypatch, entries, blobs)

    assert res == {"ok": True, "applied": 1, "rejected": 1, "conflicts": 0}
    assert (tmp_path / "md" / "captures" / "good.md").is_file()
    assert peers.load()["peers"][0].get("last_seen")


# ── 5. mesh/ is per-origin, so two folds are two identities ───────────────

def test_two_peers_folds_do_not_collide_in_mesh(monkeypatch, tmp_path):
    """Flat mesh/<key>.md meant healing a partition silently destroyed one
    leader's fold — the two entries never share an identity, so nothing was
    even reported as a conflict."""
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import apply

    for origin, text in (("alpha", "alpha fold"), ("beta", "beta fold")):
        body = _node_text("M-sync", origin, 1, origin, text)
        assert apply.apply_blob(_entry(body, kind="B", path="mesh/M-sync.md",
                                       origin=origin, key="M-sync", rev=1,
                                       updated_by=origin, derived="mesh"),
                                body) is True

    mesh = tmp_path / "md" / "mesh"
    assert b"alpha fold" in (mesh / "alpha" / "M-sync.md").read_bytes()
    assert b"beta fold" in (mesh / "beta" / "M-sync.md").read_bytes()


# ── 6. case folding ───────────────────────────────────────────────────────

def test_a_case_shifted_origin_addresses_the_same_node(monkeypatch, tmp_path):
    """`origin: "MS"` missed peers/ms/K-01.md, so the entry looked new — and on
    a case-insensitive filesystem it then wrote that very file, at a lower rev,
    bypassing the merge order."""
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import apply, paths

    node = _write(tmp_path, "peers/ms/K-01.md",
                  _node_text("K-01", "ms", 5, "ms", "current"))

    assert paths.node_paths("MS", "K-01") == [node]

    body = _node_text("K-01", "MS", 1, "MS", "downgrade")
    assert apply.apply_blob(_entry(body, kind="B", path="peers/MS/K-01.md",
                                   origin="MS", key="K-01", rev=1,
                                   updated_by="MS"), body) is False
    assert b"current" in node.read_bytes()


def test_an_uppercase_hash_still_converges(monkeypatch, tmp_path):
    """The manifest emits lowercase hex. An uppercase advert never matched, so
    the entry conflicted every cycle and could never be applied."""
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import peers

    peers.save({"self": {"id": "book", "urls": []},
                "peers": [{"id": "nuc", "urls": ["http://stub"], "state": "approved"}]})

    body = b"from nuc"
    digest = hashlib.sha256(body).hexdigest()
    entries = [{"kind": "A", "path": "captures/a.md", "hash": digest.upper()}]

    res = _sync(monkeypatch, entries, {digest: body})

    assert res["applied"] == 1
    assert (tmp_path / "md" / "captures" / "a.md").read_bytes() == body


# ── 7. the sidecar preserves the loser, not the winner ────────────────────

def test_the_losing_remote_is_sidecarred_when_the_local_wins(monkeypatch, tmp_path):
    """keep_conflict ran for every pair regardless of who won, so a local win
    filed a byte-identical copy of the live node every cycle while the remote's
    text — the only copy about to be lost — was preserved nowhere."""
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import peers

    peers.save({"self": {"id": "book", "urls": []},
                "peers": [{"id": "nuc", "urls": ["http://stub"], "state": "approved"}]})

    node = _write(tmp_path, "okf/global/learnings/L-07.md",
                  _node_text("L-07", "nuc", 47, "zeta", "local wins"))
    remote = _node_text("L-07", "nuc", 47, "alpha", "remote loses")
    entries = [_entry(remote, kind="B", path="okf/global/learnings/L-07.md",
                      origin="nuc", key="L-07", rev=47, updated_by="alpha")]

    res = _sync(monkeypatch, entries, {entries[0]["hash"]: remote})

    assert res["conflicts"] == 1
    assert b"local wins" in node.read_bytes()
    assert b"remote loses" in (node.parent / "L-07.conflict.md").read_bytes()
