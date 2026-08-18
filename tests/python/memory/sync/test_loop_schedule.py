"""The sync daemon's schedule.

Nothing schedules leadership: which machine folds is configuration
(``role.py``), so this loop only syncs. These cover the wiring, not the sync
mechanics (see test_hub_cycle).
"""
from __future__ import annotations

import contextlib


def _env(monkeypatch, tmp_path, peer_id: str = "nuc", admin: str = ""):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", peer_id)
    monkeypatch.delenv("AIFORGE_ROLE", raising=False)
    if admin:
        monkeypatch.setenv("AIFORGE_ADMIN_URL", admin)
    else:
        monkeypatch.delenv("AIFORGE_ADMIN_URL", raising=False)


def test_default_interval_is_thirty_minutes():
    from aiforge_core.memory.sync import loop

    assert loop.DEFAULT_INTERVAL == 1800


def test_an_unconfigured_cycle_does_no_network_and_returns_immediately(
        monkeypatch, tmp_path):
    """No admin url → we ARE the admin → no transport call, no manifest build,
    no blocking. An admin answers requests; it never makes them."""
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
    """An older design claimed a lease here, on a background timer. There is now
    no record to write and no thread to leak — the admin is named, not elected.
    """
    _env(monkeypatch, tmp_path, admin="http://10.0.0.9:8799")
    from aiforge_core.memory.sync import loop

    class _Stop(BaseException):
        # Not an Exception: run_forever now *swallows* those on purpose (a bad
        # cycle must not end the daemon — see test_loop_resilience), so an
        # Exception here would be caught and the loop would spin forever. The
        # assertion this test makes is unchanged.
        pass

    cycles = []

    def _once():
        cycles.append(1)
        raise _Stop

    monkeypatch.setattr(loop, "run_once", _once)
    with contextlib.suppress(_Stop):
        # Any positive interval: _once raises before the sleep is reached.
        # Zero is refused now — it made the loop spin without throttling.
        loop.run_forever(interval=1)

    assert cycles == [1]
    assert list((tmp_path / "md").rglob("*.json")) == []
