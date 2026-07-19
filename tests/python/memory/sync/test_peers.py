"""peers.json: identity, approval state, and the gossiped roster."""
from __future__ import annotations

import json


def test_load_returns_empty_when_absent(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    from aiforge_core.memory.sync import peers

    assert peers.load() == {"self": {}, "peers": []}


def test_approved_filters_out_candidates(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    from aiforge_core.memory.sync import peers

    peers.save({"self": {"id": "book"}, "peers": [
        {"id": "nuc", "urls": ["http://a"], "token": "t", "state": "approved"},
        {"id": "eve", "urls": ["http://b"], "state": "candidate"},
    ]})

    assert [p["id"] for p in peers.approved()] == ["nuc"]


def test_roster_never_exposes_tokens(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "book")
    from aiforge_core.memory.sync import peers

    peers.save({"self": {"id": "book", "urls": ["http://me"]}, "peers": [
        {"id": "nuc", "urls": ["http://a"], "token": "SECRET", "state": "approved"},
    ]})

    blob = json.dumps(peers.roster())
    assert "SECRET" not in blob
    assert {"book", "nuc"} == {r["id"] for r in peers.roster()}


def test_gossip_learns_new_peers_as_candidates(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "book")
    from aiforge_core.memory.sync import peers

    peers.save({"self": {"id": "book"}, "peers": [
        {"id": "nuc", "urls": ["http://a"], "token": "t", "state": "approved"},
    ]})
    peers.merge_roster([{"id": "alice", "urls": ["http://c"], "last_seen": 5}])

    got = {p["id"]: p for p in peers.load()["peers"]}
    assert got["alice"]["state"] == "candidate"
    assert got["alice"]["urls"] == ["http://c"]


def test_gossip_never_promotes_or_grants_a_token(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "book")
    from aiforge_core.memory.sync import peers

    peers.save({"self": {"id": "book"}, "peers": []})
    # A hostile peer claims to be approved and supplies its own token.
    peers.merge_roster([{"id": "eve", "urls": ["http://evil"],
                         "state": "approved", "token": "PWNED"}])

    got = peers.load()["peers"][0]
    assert got["state"] == "candidate"
    assert "token" not in got
    assert peers.approved() == []


def test_gossip_does_not_downgrade_an_approved_peer(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "book")
    from aiforge_core.memory.sync import peers

    peers.save({"self": {"id": "book"}, "peers": [
        {"id": "nuc", "urls": ["http://a"], "token": "t", "state": "approved"},
    ]})
    peers.merge_roster([{"id": "nuc", "urls": ["http://a", "http://b"]}])

    got = peers.load()["peers"][0]
    assert got["state"] == "approved"
    assert got["token"] == "t"
    assert got["urls"] == ["http://a", "http://b"]   # addresses do update


def test_gossip_ignores_self(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "book")
    from aiforge_core.memory.sync import peers

    peers.save({"self": {"id": "book"}, "peers": []})
    peers.merge_roster([{"id": "book", "urls": ["http://me"]}])

    assert peers.load()["peers"] == []
