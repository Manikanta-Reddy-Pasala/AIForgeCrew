"""What the daemon must outlive.

Every case here killed the process or silently ate a peer's result row. A sync
daemon that exits is worse than one that syncs nothing: the supervisor restarts
it into the same state thirty seconds later, forever, and knowledge compaction
rides this same loop, so it stops too — from a cause no log connects to a peer
file.
"""
from __future__ import annotations

import json
from pathlib import Path


def _env(monkeypatch, tmp_path, peer_id: str = "book"):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", peer_id)


def _write_registry(tmp_path, payload: dict) -> Path:
    cfg = tmp_path / "cfg"
    cfg.mkdir(parents=True, exist_ok=True)
    p = cfg / "peers.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


# ── 1. state the daemon reads must not be able to kill it ─────────────────

def test_a_malformed_registry_does_not_end_the_cycle(monkeypatch, tmp_path):
    """`{"peers": "beta"}` and `{"peers": [null]}` raised AttributeError out of
    run_once, out of run_forever, and out of the process — which came back and
    died the same way on every restart."""
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import loop

    for payload in ({"peers": "beta"}, {"peers": [None]}, {"peers": [["x"]]},
                    {"self": "nope", "peers": [{"id": "a"}]}):
        _write_registry(tmp_path, payload)
        assert isinstance(loop.run_once(), list), payload


def test_a_failing_discovery_sweep_does_not_end_the_cycle(monkeypatch, tmp_path):
    """Injecting ENOSPC with SSDP on gave `run_once RAISED OSError [Errno 28]`:
    the sweep's own merge_roster write sits outside every per-peer try."""
    _env(monkeypatch, tmp_path)
    monkeypatch.setenv("AIFORGE_SYNC_SSDP", "1")
    monkeypatch.setenv("AIFORGE_SYNC_SSDP_HOST", "127.0.0.1")
    from aiforge_core.memory.sync import discovery_ssdp, loop, peers

    monkeypatch.setattr(discovery_ssdp, "discover", lambda *_a, **_k: [{"id": "x"}])
    monkeypatch.setattr(peers, "merge_roster",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError(28, "No space")))

    assert loop.run_once() == []


def test_run_forever_survives_a_cycle_that_raises(monkeypatch, tmp_path):
    """`while True: run_once()` had no try at all, so one bad cycle ended the
    daemon — and with it the compaction pass that runs in the same loop body."""
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.okf import tiers
    from aiforge_core.memory.sync import loop

    class _Stop(BaseException):
        pass

    slept = []

    def _boom():
        raise RuntimeError("cycle exploded")

    def _sleep(seconds):
        slept.append(seconds)
        raise _Stop            # one iteration is all we need to observe

    monkeypatch.setattr(loop, "run_once", _boom)
    monkeypatch.setattr(tiers, "run_after_sync", lambda *_a, **_k: None)
    monkeypatch.setattr(loop.time, "sleep", _sleep)

    try:
        loop.run_forever(interval=7)
    except _Stop:
        pass
    else:                      # pragma: no cover — _sleep always raises
        raise AssertionError("run_forever returned instead of looping")

    assert slept == [7], "a raising cycle must reach the sleep, not the exit"


# ── 2. a peer on another build must not take the cycle down ───────────────

def test_a_wrong_shaped_manifest_response_still_advances_last_seen(
        monkeypatch, tmp_path):
    """A peer answering `{"manifest": {"not":"a list"}, "roster": "nope"}` blew
    up on the roster merge *after* the applies: peers.touch never ran, so its
    last_seen froze, it aged out of the election, and its result row vanished
    from the cycle output entirely."""
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import peers, transport

    peers.save({"self": {"id": "book", "urls": []},
                "peers": [{"id": "nuc", "urls": ["http://stub"],
                           "state": peers.STATE_APPROVED, "last_seen": 0}]})

    body = json.dumps({"manifest": {"not": "a list"}, "roster": "nope"}).encode()
    monkeypatch.setattr(transport, "_fetch", lambda *_a, **_k: body)

    from aiforge_core.memory.sync import loop

    rows = loop.run_once()

    assert len(rows) == 1 and rows[0]["peer"] == "nuc"
    assert rows[0]["ok"] is True
    assert peers.load()["peers"][0]["last_seen"] > 0


def test_a_peer_row_survives_bookkeeping_that_raises(monkeypatch, tmp_path):
    """Under a global ENOSPC the entries were correctly counted as rejected and
    then merge_roster raised, throwing the whole row away: the cycle reported
    `[]` and one WARNING was the only evidence the peer had been tried."""
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import loop, peers, transport

    peers.save({"self": {"id": "book", "urls": []},
                "peers": [{"id": "nuc", "urls": ["http://stub"],
                           "state": peers.STATE_APPROVED, "last_seen": 0}]})

    monkeypatch.setattr(transport, "fetch_manifest",
                        lambda *_a, **_k: {"manifest": [
                            {"kind": "A", "path": "captures/a.md", "hash": "ab"}],
                            "roster": []})
    monkeypatch.setattr(transport, "fetch_blob", lambda *_a, **_k: None)
    monkeypatch.setattr(peers, "merge_roster",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError(28, "No space")))

    rows = loop.run_once()

    assert len(rows) == 1
    assert rows[0]["peer"] == "nuc"
    assert rows[0]["rejected"] == 1, "counts earned before the failure are kept"
