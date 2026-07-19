"""The sync daemon's schedule.

Nothing schedules leadership: the compaction leader is elected from the peer
registry on demand (``election.py``), so this loop only pulls. These cover the
wiring, not the sync mechanics (see test_two_peer).
"""
from __future__ import annotations

import contextlib


def _env(monkeypatch, tmp_path, peer_id: str = "nuc"):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", peer_id)


def _approve(peer_id: str = "book") -> None:
    from aiforge_core.memory.sync import peers

    data = peers.load()
    data["peers"] = [{"id": peer_id, "urls": ["http://10.0.0.9:8799"],
                      "state": peers.STATE_APPROVED, "last_seen": 0}]
    peers.save(data)


def test_default_interval_is_thirty_minutes():
    from aiforge_core.memory.sync import loop

    assert loop.DEFAULT_INTERVAL == 1800


def test_an_unconfigured_cycle_does_no_network_and_returns_immediately(
        monkeypatch, tmp_path):
    """No approved peers → no transport call, no manifest build, no blocking."""
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import loop, manifest, transport

    def _boom(*a, **k):  # pragma: no cover — the point is that it never runs
        raise AssertionError("idle cycle touched the network / built a manifest")

    monkeypatch.setattr(transport, "fetch_manifest", _boom)
    monkeypatch.setattr(transport, "fetch_blob", _boom)
    monkeypatch.setattr(manifest, "build", _boom)

    assert loop.run_once() == []


def test_run_forever_only_runs_cycles_and_holds_no_leadership_record(
        monkeypatch, tmp_path):
    """The old design claimed a lease here, on a background timer. There is now
    no record to write and no thread to leak — leadership is derived, not held.
    """
    _env(monkeypatch, tmp_path)
    _approve()
    from aiforge_core.memory.sync import loop

    class _Stop(Exception):
        pass

    cycles = []

    def _once():
        cycles.append(1)
        raise _Stop

    monkeypatch.setattr(loop, "run_once", _once)
    with contextlib.suppress(_Stop):
        loop.run_forever(interval=0)

    assert cycles == [1]
    assert list((tmp_path / "md").rglob("*.json")) == []
