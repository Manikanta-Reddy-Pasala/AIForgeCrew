"""Leader election — deterministic, computed from replicated data, no clocks.

The design this replaced was a wall-clock lease, and the test it never had is
:func:`test_a_peers_state_arriving_a_full_cycle_late_still_elects_one_leader`.
"""
from __future__ import annotations

import time

import pytest


def _env(monkeypatch, tmp_path, peer_id: str = "nuc"):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", peer_id)


def _approve(*entries: tuple[str, int]) -> None:
    """Approve peers as ``(id, seconds_since_we_last_reached_them)``."""
    from aiforge_core.memory.sync import peers

    now = int(time.time())
    data = peers.load()
    data["peers"] = [{"id": pid, "urls": [f"http://{pid}:8799"],
                      "state": peers.STATE_APPROVED, "last_seen": now - ago}
                     for pid, ago in entries]
    peers.save(data)


def test_a_lone_machine_leads_itself(monkeypatch, tmp_path):
    """The default install: no peers, no mesh, unchanged behaviour."""
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import election

    assert election.candidates() == ["nuc"]
    assert election.leader() == "nuc"
    assert election.is_leader() is True
    assert election.may_distil() is True


def test_the_lowest_id_among_live_peers_leads(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path, "nuc")
    _approve(("book", 60), ("air", 60))
    from aiforge_core.memory.sync import election

    assert election.candidates() == ["air", "book", "nuc"]
    assert election.leader() == "air"
    assert election.is_leader() is False
    assert election.may_distil() is False


def test_the_lowest_id_itself_leads_and_the_others_do_not(monkeypatch, tmp_path):
    """Same three peers, computed from each one's own registry: exactly one leads."""
    from aiforge_core.memory.sync import election

    leaders = []
    for me, others in (("air", ("book", "nuc")), ("book", ("air", "nuc")),
                       ("nuc", ("air", "book"))):
        _env(monkeypatch, tmp_path / me, me)
        _approve(*[(o, 60) for o in others])
        if election.is_leader():
            leaders.append(me)

    assert leaders == ["air"]


def test_a_peer_we_have_never_reached_is_not_a_candidate(monkeypatch, tmp_path):
    """last_seen 0 = approved in config but never answered. It may not exist."""
    _env(monkeypatch, tmp_path, "nuc")
    _approve(("air", 0))          # rewritten to "now", then zeroed below
    from aiforge_core.memory.sync import peers

    data = peers.load()
    data["peers"][0]["last_seen"] = 0
    peers.save(data)
    from aiforge_core.memory.sync import election

    assert election.candidates() == ["nuc"]
    assert election.is_leader() is True


def test_a_stale_peer_is_not_a_candidate(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path, "nuc")
    from aiforge_core.memory.sync import election
    _approve(("air", election.ALIVE_WINDOW + 1))

    assert election.candidates() == ["nuc"]
    assert election.is_leader() is True


def test_a_silent_leader_hands_over_to_the_next_id(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path, "nuc")
    from aiforge_core.memory.sync import election

    # air leads while we can still reach it…
    _approve(("air", 60), ("book", 60))
    assert election.leader() == "air"

    # …and once it has been silent past the window, book takes over.
    _approve(("air", election.ALIVE_WINDOW + 60), ("book", 60))
    assert election.leader() == "book"
    assert election.is_leader() is False


def test_the_alive_window_comfortably_exceeds_the_sync_interval():
    """One missed cycle must not flap leadership; three cycles of slack."""
    from aiforge_core.memory.sync import election, loop

    assert election.ALIVE_WINDOW == 3 * loop.DEFAULT_INTERVAL


def test_a_peers_state_arriving_a_full_cycle_late_still_elects_one_leader(
        monkeypatch, tmp_path):
    """THE regression test for the clock-based lease.

    A peer's record only reaches us on the 1800s pull cycle, so everything we
    know about ``air`` is at least that old — which is precisely what made a
    600s TTL read as "expired, free to claim" on every peer at once. Age alone
    must not change who leads.
    """
    from aiforge_core.memory.sync import election, loop

    stale = loop.DEFAULT_INTERVAL      # exactly one cycle of replication lag
    leaders = []
    for me, other in (("air", "nuc"), ("nuc", "air")):
        _env(monkeypatch, tmp_path / me, me)
        _approve((other, stale))
        if election.is_leader():
            leaders.append(me)

    assert leaders == ["air"], "both peers thought they were the leader"


def test_the_election_never_reads_a_foreign_clock(monkeypatch, tmp_path):
    """``last_seen`` is OUR observation of a peer (peers.touch), never theirs.

    Proof: gossip carrying a wildly-skewed stamp for a peer we cannot reach
    changes nothing — it is dropped on the way in, so the peer stays stale and
    we stay leader.
    """
    _env(monkeypatch, tmp_path, "nuc")
    from aiforge_core.memory.sync import election, peers
    _approve(("air", election.ALIVE_WINDOW + 600))

    peers.merge_roster([{"id": "air", "urls": ["http://air:8799"],
                         "last_seen": int(time.time()) + 10_000_000}])

    assert election.candidates() == ["nuc"]
    assert election.is_leader() is True


def test_a_miscased_roster_entry_still_elects_exactly_one_leader(
        monkeypatch, tmp_path):
    """BLOCKER regression: a hand-typed id elects a machine that does not exist.

    Both peers call themselves lowercase; a human approved each of them on the
    other side with the capitalisation they wrote in their notes. Uppercase
    sorts before lowercase, so each peer concluded the *other* led, ``mesh/``
    was never written by anybody, and every peer's view returned ``no-mesh``
    forever — with one log line as the only trace.
    """
    from aiforge_core.memory.sync import election

    leaders = []
    for me, other in (("nuc-prod", "Book-Air"), ("book-air", "NUC-Prod")):
        _env(monkeypatch, tmp_path / me, me)
        _approve((other, 60))
        leaders.append(election.leader())

    assert leaders == ["book-air", "book-air"], "the two peers disagree"


def test_a_last_seen_in_the_future_does_not_keep_a_dead_peer_alive(
        monkeypatch, tmp_path):
    """``now - seen`` is negative under clock skew, and an upper bound alone
    accepts that forever: a peer stamped two years ahead leads for two years."""
    _env(monkeypatch, tmp_path, "nuc")
    from aiforge_core.memory.sync import election
    _approve(("air", -2 * 365 * 24 * 3600))       # stamped two years ahead

    assert election.candidates() == ["nuc"]
    assert election.is_leader() is True


def test_a_broken_registry_makes_us_distil_anyway(monkeypatch, tmp_path):
    """Soft-fail OPEN: losing distillation is worse than duplicating it."""
    _env(monkeypatch, tmp_path, "nuc")
    _approve(("air", 60))
    from aiforge_core.memory.sync import election

    monkeypatch.setattr(election, "is_leader",
                        lambda: (_ for _ in ()).throw(OSError("config gone")))

    assert election.may_distil() is True


def test_naming_the_leader_never_raises(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path, "nuc")
    from aiforge_core.memory.sync import election

    monkeypatch.setattr(election, "leader",
                        lambda: (_ for _ in ()).throw(OSError("config gone")))

    assert election.leader_name() == "?"


@pytest.mark.parametrize("raw", [None, "", "not-a-number", []])
def test_a_hand_edited_last_seen_never_takes_the_election_down(
        monkeypatch, tmp_path, raw):
    _env(monkeypatch, tmp_path, "nuc")
    from aiforge_core.memory.sync import election, peers

    peers.save({"self": {"id": "nuc"}, "peers": [
        {"id": "air", "urls": ["http://air"], "state": peers.STATE_APPROVED,
         "last_seen": raw}]})

    assert election.candidates() == ["nuc"]
