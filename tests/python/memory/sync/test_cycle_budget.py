"""What one cycle may cost, and who is allowed to spend it.

Per-request deadlines bound a single request; they never bounded their sum.
MAX_PEERS is 64 and one peer costs a manifest plus up to MAX_MANIFEST_ENTRIES
(20 000) blob fetches, so "check the budget after each peer returns" is not a
budget: it cannot preempt the peer that is actually spending it. Everything
here drives a peer whose own cost exceeds the whole budget — a stub that
finishes inside the budget proves only that the budget stops *starting* peers.
"""
from __future__ import annotations

import time

import pytest

BLOB_DELAY = 0.25
ENTRIES = 20                 # one peer alone: 20 x 0.25s = 5.0s
BUDGET = 1.0


def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "book")


def _approve(n: int):
    from aiforge_core.memory.sync import peers

    peers.save({"self": {"id": "book", "urls": []},
                "peers": [{"id": f"p{i}", "urls": [f"http://stub{i}"],
                           "state": peers.STATE_APPROVED, "last_seen": 0}
                          for i in range(n)]})


def _fat_manifest(*_a, **_k):
    return {"manifest": [{"kind": "A", "path": f"captures/{i:03d}.md",
                          "hash": f"{i:064x}"} for i in range(ENTRIES)],
            "roster": []}


def _slow_blob(*_a, **_k):
    time.sleep(BLOB_DELAY)
    return None              # counted as rejected; keeps the test on timing


@pytest.fixture
def sick_peers(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import loop, transport

    _approve(3)
    monkeypatch.setattr(transport, "fetch_manifest", _fat_manifest)
    monkeypatch.setattr(transport, "fetch_blob", _slow_blob)
    monkeypatch.setattr(loop, "CYCLE_BUDGET", BUDGET)
    return loop


def test_one_peer_cannot_run_the_cycle_past_the_budget(sick_peers):
    """The first peer's own work is 5x the entire budget. Sampling the budget
    only after ``sync_with`` returns cannot stop it: the worst case was the
    budget plus one peer's full cost, and a full cost is 20 000 blobs."""
    started = time.monotonic()
    rows = sick_peers.run_once()
    elapsed = time.monotonic() - started

    assert elapsed < BUDGET + 2.0, (
        f"cycle ran {elapsed:.1f}s on a {BUDGET}s budget — one peer alone "
        f"costs {ENTRIES * BLOB_DELAY:.1f}s")


def test_the_budget_preempts_a_peer_part_way_through_its_manifest(sick_peers):
    """Preemption has to happen *inside* the blob loop. Checking only between
    peers means a peer that passes the pre-flight check by a millisecond still
    gets to spend an unbounded amount of the cycle."""
    rows = sick_peers.run_once()

    assert rows[0]["rejected"] < ENTRIES, (
        "the first peer fetched its whole manifest despite the budget "
        f"({rows[0]['rejected']}/{ENTRIES} entries)")
    assert rows[0]["rejected"] > 0, "it should still have made progress"


def test_peers_dropped_for_budget_are_reported_not_silently_absent(sick_peers):
    """A peer that never got its turn must appear in the cycle output; the
    admin page cannot distinguish 'skipped' from 'never configured' otherwise."""
    rows = sick_peers.run_once()

    assert [r["peer"] for r in rows] == ["p0", "p1", "p2"]
    assert not rows[0].get("skipped")
    assert all(r.get("skipped") for r in rows[1:])


def test_a_slow_discovery_sweep_is_charged_to_the_cycle_budget(
        monkeypatch, tmp_path):
    """``started`` used to be sampled *after* ``_ssdp_sweep``, so a multicast
    wait that ate the whole interval still handed the peers a full budget and
    the cycle overran by the sweep's entire cost."""
    _env(monkeypatch, tmp_path)
    monkeypatch.setenv("AIFORGE_SYNC_SSDP", "1")
    monkeypatch.setenv("AIFORGE_SYNC_SSDP_HOST", "127.0.0.1")
    from aiforge_core.memory.sync import discovery_ssdp, loop, transport

    _approve(3)
    monkeypatch.setattr(discovery_ssdp, "discover",
                        lambda *_a, **_k: (time.sleep(BUDGET + 0.5), [])[1])
    monkeypatch.setattr(transport, "fetch_manifest", _fat_manifest)
    monkeypatch.setattr(transport, "fetch_blob", _slow_blob)
    monkeypatch.setattr(loop, "CYCLE_BUDGET", BUDGET)

    started = time.monotonic()
    rows = loop.run_once()
    elapsed = time.monotonic() - started

    assert all(r.get("skipped") for r in rows), (
        "the sweep spent the whole budget, so no peer should have been started")
    assert elapsed < BUDGET + 2.0, f"cycle ran {elapsed:.1f}s"
