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
    # Addresses do NOT update: a known peer's url is out-of-band configuration
    # exactly like its token (see test_gossip_cannot_repoint_a_known_peer).
    assert got["urls"] == ["http://a"]


def test_gossip_cannot_repoint_a_known_peer(monkeypatch, tmp_path):
    """BLOCKER regression: a gossiped url exfiltrates the peer's bearer token.

    An approved low-value peer gossips a roster claiming a *different* peer now
    lives at an address it controls. The registry kept ``approved`` and the real
    token but pointed at the attacker, so the next pull handed the attacker that
    peer's bearer token — full access to its control plane — and made the
    attacker that peer for sync purposes.
    """
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "book")
    from aiforge_core.memory.sync import loop, peers, transport

    peers.save({"self": {"id": "book"}, "peers": [
        {"id": "ms", "urls": ["http://ms.lan:8799"], "token": "SUPER-SECRET-MS-TOKEN",
         "state": "approved"},
    ]})

    peers.merge_roster([{"id": "ms", "urls": ["http://attacker.lan:9/"]}])

    got = peers.load()["peers"][0]
    assert got["urls"] == ["http://ms.lan:8799"]
    assert got["state"] == "approved"
    assert got["token"] == "SUPER-SECRET-MS-TOKEN"

    # …and the token is never carried to the gossiped address.
    seen = []
    monkeypatch.setattr(transport, "fetch_manifest",
                        lambda base, token="": seen.append((base, token)) or {})
    loop.sync_with(peers.approved()[0])
    assert seen == [("http://ms.lan:8799", "SUPER-SECRET-MS-TOKEN")]


def test_gossip_cannot_grow_the_registry_past_the_cap(monkeypatch, tmp_path):
    """One hostile roster added 200+ entries, which the admin page then probes."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "book")
    from aiforge_core.memory.sync import peers

    peers.save({"self": {"id": "book"}, "peers": []})
    hostile = [{"id": f"bot{i}", "urls": [f"http://bot{i}"]} for i in range(250)]

    peers.merge_roster(hostile)
    assert len(peers.load()["peers"]) == peers.MAX_NEW_PER_MERGE

    for _ in range(50):                    # …and it cannot creep there either
        peers.merge_roster(hostile)
    assert len(peers.load()["peers"]) == peers.MAX_PEERS


def test_one_machine_cannot_occupy_two_roster_rows(monkeypatch, tmp_path):
    """A human typed ``NUC-Prod``; the machine calls itself ``nuc-prod``."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "book")
    from aiforge_core.memory.sync import peers

    peers.save({"self": {"id": "book"}, "peers": [
        {"id": "NUC-Prod", "urls": ["http://nuc"], "token": "t", "state": "approved"},
    ]})
    peers.merge_roster([{"id": "nuc-prod", "urls": ["http://elsewhere"]}])

    rows = peers.load()["peers"]
    assert [r["id"] for r in rows] == ["nuc-prod"]
    assert rows[0]["state"] == "approved"
    assert rows[0]["urls"] == ["http://nuc"]


def test_touch_clamps_a_last_seen_written_ahead_of_our_clock(monkeypatch, tmp_path):
    """A stamp from a bad RTC or a hand-edited file never expires on its own."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "book")
    import time

    from aiforge_core.memory.sync import peers

    future = int(time.time()) + 10_000_000
    peers.save({"self": {"id": "book"}, "peers": [
        {"id": "nuc", "urls": ["http://a"], "token": "t", "state": "approved",
         "last_seen": future},
        {"id": "air", "urls": ["http://b"], "token": "t", "state": "approved",
         "last_seen": 0},
    ]})
    peers.touch("air")

    got = {p["id"]: p["last_seen"] for p in peers.load()["peers"]}
    assert got["nuc"] <= int(time.time())
    assert got["air"] >= int(time.time()) - 5


def test_gossip_ignores_self(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "book")
    from aiforge_core.memory.sync import peers

    peers.save({"self": {"id": "book"}, "peers": []})
    peers.merge_roster([{"id": "book", "urls": ["http://me"]}])

    assert peers.load()["peers"] == []
