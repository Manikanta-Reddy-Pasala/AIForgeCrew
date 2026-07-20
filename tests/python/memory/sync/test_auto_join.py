"""Shared-key auto-join: a candidate that proves it holds AIFORGE_MESH_KEY is
promoted to approved with no human step (mode A). Possession of the shared
secret IS mesh membership; the human token-copy is only the fallback when no
mesh key is set.

These tests drive ``peers`` (the registry) and ``loop._auto_promote`` (the
gate) directly, stubbing ``transport.fetch_manifest`` to stand in for a peer
that either accepts or rejects our key — the auth of the key itself is covered
end-to-end in tests/python/api/test_sync_endpoint_auth.py.
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def peers(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "book")
    monkeypatch.delenv("AIFORGE_MESH_KEY", raising=False)
    from aiforge_core.memory.sync import peers as p
    importlib.reload(p)
    return p


def _seed_candidate(peers, pid="nuc", url="http://nuc:8799"):
    data = peers.load()
    data["peers"].append({"id": pid, "urls": [url],
                          "state": peers.STATE_CANDIDATE, "last_seen": 0})
    peers.save(data)


def _state(peers, pid):
    for row in peers.load()["peers"]:
        if peers.normalise_id(row.get("id")) == peers.normalise_id(pid):
            return row.get("state")
    return None


# ── peers.promote ────────────────────────────────────────────────────────

def test_promote_flips_candidate_to_approved(peers):
    _seed_candidate(peers)
    assert peers.promote("nuc") is True
    assert _state(peers, "nuc") == peers.STATE_APPROVED


def test_promote_is_idempotent(peers):
    _seed_candidate(peers)
    assert peers.promote("nuc") is True
    assert peers.promote("nuc") is False           # already approved, no change


def test_promote_adopts_a_url_only_when_the_row_has_none(peers):
    data = peers.load()
    data["peers"].append({"id": "nuc", "urls": ["http://real:8799"],
                          "state": peers.STATE_CANDIDATE, "last_seen": 0})
    peers.save(data)
    peers.promote("nuc", url="http://attacker:8799")
    row = next(r for r in peers.load()["peers"]
               if peers.normalise_id(r["id"]) == "nuc")
    assert row["urls"] == ["http://real:8799"]     # operator address not re-pointed


def test_promote_of_an_unknown_peer_is_a_noop(peers):
    assert peers.promote("ghost") is False


# ── loop._auto_promote (the gate) ──────────────────────────────────────────

def _stub_transport(monkeypatch, *, server_key):
    """A peer that answers the challenge with HMAC(server_key, nonce) — exactly
    what a peer holding ``server_key`` returns. The prober never sends a key, so
    a mismatch is the peer simply not holding ours."""
    import hashlib
    import hmac as _hmac

    def _proof(base_url, nonce):
        if server_key is None:
            return ""          # peer offers no proof at all (no mesh key)
        return _hmac.new(server_key.encode(), nonce.encode(),
                         hashlib.sha256).hexdigest()
    monkeypatch.setattr(
        "aiforge_core.memory.sync.transport.membership_proof", _proof)


def test_a_candidate_holding_the_shared_key_auto_joins(peers, monkeypatch):
    monkeypatch.setenv("AIFORGE_MESH_KEY", "shared")
    _seed_candidate(peers)
    _stub_transport(monkeypatch, server_key="shared")
    from aiforge_core.memory.sync import loop
    loop._auto_promote()
    assert _state(peers, "nuc") == peers.STATE_APPROVED


def test_a_candidate_with_a_different_key_stays_a_candidate(peers, monkeypatch):
    monkeypatch.setenv("AIFORGE_MESH_KEY", "ours")
    _seed_candidate(peers)
    _stub_transport(monkeypatch, server_key="theirs")   # our key is refused
    from aiforge_core.memory.sync import loop
    loop._auto_promote()
    assert _state(peers, "nuc") == peers.STATE_CANDIDATE


def test_no_mesh_key_means_no_auto_join(peers, monkeypatch):
    # Even a peer that would prove any key is not probed: without a configured
    # mesh key, approval stays a human step.
    _seed_candidate(peers)
    _stub_transport(monkeypatch, server_key="anything")
    from aiforge_core.memory.sync import loop
    loop._auto_promote()
    assert _state(peers, "nuc") == peers.STATE_CANDIDATE


def test_a_hostile_candidate_never_receives_our_key(peers, monkeypatch):
    """The reason auto-join is challenge-response: probing a candidate url from
    untrusted SSDP/gossip must not hand it the shared secret. The prober calls
    membership_proof, which sends NO credential — assert that."""
    monkeypatch.setenv("AIFORGE_MESH_KEY", "top-secret")
    _seed_candidate(peers)
    seen = {}

    def _proof(base_url, nonce):
        seen["nonce"] = nonce
        return "deadbeef"          # a wrong proof — attacker cannot forge ours
    monkeypatch.setattr(
        "aiforge_core.memory.sync.transport.membership_proof", _proof)
    from aiforge_core.memory.sync import loop
    loop._auto_promote()
    assert _state(peers, "nuc") == peers.STATE_CANDIDATE   # not promoted
    assert "top-secret" not in str(seen)                   # key never sent


def test_auto_promote_respects_the_cycle_deadline(peers, monkeypatch):
    import time
    monkeypatch.setenv("AIFORGE_MESH_KEY", "shared")
    _seed_candidate(peers, pid="nuc")
    _seed_candidate(peers, pid="air", url="http://air:8799")
    _stub_transport(monkeypatch, server_key="shared")
    from aiforge_core.memory.sync import loop
    loop._auto_promote(deadline=time.monotonic() - 1)   # already spent
    # Budget spent before any probe: nothing promoted this cycle.
    assert _state(peers, "nuc") == peers.STATE_CANDIDATE
    assert _state(peers, "air") == peers.STATE_CANDIDATE
