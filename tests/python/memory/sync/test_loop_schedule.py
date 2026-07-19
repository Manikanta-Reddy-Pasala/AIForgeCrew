"""The sync daemon's schedule and its lease heartbeat.

Two things were shipped unwired: nothing ran the cycle, and nothing held the
lease. These cover the wiring, not the sync mechanics (see test_two_peer).
"""
from __future__ import annotations

import contextlib
import json


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


class _FakeThread:
    """Stands in for the heartbeat thread; never actually runs.

    A real one would outlive the test and keep ticking against a deleted tmp
    config — the tick itself is exercised directly instead.
    """

    def __init__(self, target):
        self.target = target
        self.daemon = True

    def is_alive(self):
        return True


def _no_threads(monkeypatch) -> list:
    """Capture heartbeat spawns instead of starting them. Returns the log."""
    from aiforge_core.memory.sync import lease

    spawned: list = []

    def _fake(run):
        t = _FakeThread(run)
        spawned.append(t)
        return t

    monkeypatch.setattr(lease, "_spawn", _fake)
    monkeypatch.setattr(lease, "_heartbeat", None)
    return spawned


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


def test_run_forever_claims_the_lease_before_the_first_cycle(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _approve()
    from aiforge_core.memory.sync import lease, loop
    _no_threads(monkeypatch)

    class _Stop(Exception):
        pass

    def _once():
        raise _Stop

    monkeypatch.setattr(loop, "run_once", _once)
    with contextlib.suppress(_Stop):
        loop.run_forever(interval=0)

    assert lease.is_holder() is True


def test_the_lease_survives_longer_than_its_ttl(monkeypatch, tmp_path):
    """A renew driven by the 30-minute cycle would let a 10-minute TTL lapse.

    The heartbeat runs on its own RENEW_EVERY timer, so simulating elapsed
    wall-clock between ticks must still leave us holding it.
    """
    _env(monkeypatch, tmp_path)
    _approve()
    import time as _time

    from aiforge_core.memory.sync import lease

    assert lease.claim() is True
    now = int(_time.time())
    # Walk forward past two full TTLs, ticking only as often as the heartbeat
    # would (RENEW_EVERY = 180s, i.e. three ticks per TTL).
    for step in range(180, 2 * lease.TTL, lease.RENEW_EVERY):
        monkeypatch.setattr(lease.time, "time", lambda s=step: now + s)
        lease._heartbeat_tick()
        assert lease.is_holder() is True, f"lease lapsed after {step}s"

    rec = json.loads((tmp_path / "md" / "okf" / ".lease.json").read_text())
    assert rec["holder"] == "nuc"


def test_a_dead_leaders_lease_is_picked_up_by_whoever_is_still_running(
        monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path, "book")
    _approve("nuc")
    from aiforge_core.memory.sync import lease

    path = tmp_path / "md" / "okf" / ".lease.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"key": "__lease__", "rev": 4, "holder": "nuc",
                                "expires_at": 1}), encoding="utf-8")

    lease._heartbeat_tick()

    assert lease.is_holder() is True
    assert json.loads(path.read_text())["holder"] == "book"


def test_the_heartbeat_writes_nothing_on_a_single_machine(monkeypatch, tmp_path):
    """No peers → no lease record at all; the file is pure mesh bookkeeping."""
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import lease

    lease._heartbeat_tick()

    assert not (tmp_path / "md" / "okf" / ".lease.json").exists()


def test_start_heartbeat_claims_synchronously_and_starts_one_daemon(
        monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _approve()
    from aiforge_core.memory.sync import lease
    spawned = _no_threads(monkeypatch)

    lease.start_heartbeat()
    assert lease.is_holder() is True          # claimed on THIS thread
    assert len(spawned) == 1 and spawned[0].daemon is True

    lease.start_heartbeat()
    assert len(spawned) == 1                  # idempotent, not a thread leak


def test_a_broken_lease_heartbeat_does_not_take_the_daemon_down(
        monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _approve()
    from aiforge_core.memory.sync import lease

    monkeypatch.setattr(lease, "_heartbeat_tick",
                        lambda: (_ for _ in ()).throw(OSError("disk gone")))
    lease._tick_safely()      # must not raise
