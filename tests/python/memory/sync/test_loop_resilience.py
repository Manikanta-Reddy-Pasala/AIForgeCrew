"""What the daemon must outlive.

Every case here killed the process or silently ate a peer's result row. A sync
daemon that exits is worse than one that syncs nothing: the supervisor restarts
it into the same state thirty seconds later, forever, and knowledge compaction
rides this same loop, so it stops too — from a cause no log connects to a peer
file.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# A row that must survive whatever else is in the file beside it. Every test
# below asserts on *this* peer: "run_once returned a list" and "run_once
# returned []" are both satisfied by dropping every healthy peer, which is the
# actual production failure, so neither is an assertion.
HEALTHY = {"id": "nuc", "urls": ["http://stub"], "state": "approved",
           "last_seen": 0}


def _reachable(monkeypatch):
    """Make HEALTHY answer, so "was it contacted" is observable in the row."""
    from aiforge_core.memory.sync import transport

    monkeypatch.setattr(transport, "fetch_manifest",
                        lambda *_a, **_k: {"manifest": [], "roster": []})


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


def test_one_malformed_row_does_not_cost_the_healthy_peers(monkeypatch, tmp_path):
    """`approved()` did `p.get("state")` over every row, so a single stray
    string — peers.json is hand-edited and gossip-fed — raised, run_once caught
    it as "the registry is unreadable", and *every healthy peer* was dropped.
    Silently, every cycle, forever, taking compaction down with it."""
    _env(monkeypatch, tmp_path)
    _reachable(monkeypatch)
    from aiforge_core.memory.sync import loop

    for bad in ("i-am-a-string", None, ["x"], 7, 3.5):
        _write_registry(tmp_path, {"self": {"id": "book", "urls": []},
                                   "peers": [bad, dict(HEALTHY)]})
        rows = loop.run_once()

        assert [r["peer"] for r in rows] == ["nuc"], bad
        assert rows[0]["ok"] is True, f"healthy peer not contacted beside {bad!r}"


def test_a_malformed_row_does_not_freeze_a_healthy_peers_last_seen(
        monkeypatch, tmp_path):
    """Confining the damage means the *whole* pull survives, not just its start:
    `merge_roster` and `touch` run after the blobs are applied and both walked
    the same rows, so the bad row still aborted the pull at the bookkeeping —
    last_seen froze, the peer aged out of the election, and the sync that had
    actually succeeded left no trace of it."""
    _env(monkeypatch, tmp_path)
    _reachable(monkeypatch)
    from aiforge_core.memory.sync import loop, peers

    _write_registry(tmp_path, {"self": {"id": "book", "urls": []},
                               "peers": ["junk", dict(HEALTHY)]})

    assert loop.run_once()[0]["ok"] is True

    rows = [p for p in peers.load()["peers"] if isinstance(p, dict)]
    assert rows[0]["last_seen"] > 0, "a successful pull must stamp last_seen"


def test_a_failing_discovery_sweep_does_not_end_the_cycle(monkeypatch, tmp_path):
    """Injecting ENOSPC with SSDP on gave `run_once RAISED OSError [Errno 28]`:
    the sweep's own merge_roster write sits outside every per-peer try.

    The sweep then shared its `try` with the registry read, so its failure was
    indistinguishable from an unreadable peers.json and cost the cycle every
    peer — for a step whose only output is candidates this cycle will not pull
    from anyway. Discovery is best-effort; the peers are not.
    """
    _env(monkeypatch, tmp_path)
    monkeypatch.setenv("AIFORGE_SYNC_SSDP", "1")
    monkeypatch.setenv("AIFORGE_SYNC_SSDP_HOST", "127.0.0.1")
    _reachable(monkeypatch)
    from aiforge_core.memory.sync import discovery_ssdp, loop, peers

    _write_registry(tmp_path, {"self": {"id": "book", "urls": []},
                               "peers": [dict(HEALTHY)]})
    monkeypatch.setattr(discovery_ssdp, "discover", lambda *_a, **_k: [{"id": "x"}])
    monkeypatch.setattr(peers, "merge_roster",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError(28, "No space")))

    rows = loop.run_once()

    assert [r["peer"] for r in rows] == ["nuc"], "a failed sweep ate the peers"
    assert rows[0]["ok"] is True


def test_run_forever_survives_a_cycle_that_raises(monkeypatch, tmp_path):
    """`while True: run_once()` had no try at all, so one bad cycle ended the
    daemon — and with it the compaction pass that runs in the same loop body."""
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.okf import tiers
    from aiforge_core.memory.sync import loop

    def _boom():
        raise RuntimeError("cycle exploded")

    monkeypatch.setattr(loop, "run_once", _boom)
    monkeypatch.setattr(tiers, "run_after_sync", lambda *_a, **_k: None)
    slept = _stop_at_sleep(monkeypatch, loop)   # one iteration is all we observe

    try:
        loop.run_forever(interval=7)
    except _Stop:
        pass
    else:                      # pragma: no cover — _sleep always raises
        raise AssertionError("run_forever returned instead of looping")

    assert slept == [7], "a raising cycle must reach the sleep, not the exit"


class _Stop(BaseException):
    """Ends run_forever from inside its sleep. A BaseException on purpose: an
    ordinary Exception is what the loop is built to swallow, so raising one
    would make the loop spin instead of stopping — a test that ends the daemon
    the way production never does tests nothing."""


def _stop_at_sleep(monkeypatch, loop, after: int = 1) -> list:
    slept: list = []

    def _sleep(seconds):
        slept.append(seconds)
        if len(slept) >= after:
            raise _Stop
    monkeypatch.setattr(loop.time, "sleep", _sleep)
    return slept


def test_a_non_positive_interval_is_refused_before_any_cycle_runs(
        monkeypatch, tmp_path):
    """`--interval` reached run_forever unvalidated. 0 turned the blanket
    `except` into an unthrottled traceback firehose; a negative value raised
    ValueError out of `time.sleep` — the one line the try cannot cover — and
    killed a daemon whose entire design is to outlive its own failures."""
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.okf import tiers
    from aiforge_core.memory.sync import loop

    ran: list = []
    monkeypatch.setattr(loop, "run_once", lambda: ran.append(1) or [])
    monkeypatch.setattr(tiers, "run_after_sync", lambda *_a, **_k: None)
    _stop_at_sleep(monkeypatch, loop)

    for bad in (0, -1, -1800):
        with pytest.raises(ValueError):
            loop.run_forever(interval=bad)

    assert ran == [], "a bad interval must be refused before the daemon starts"


def test_the_cli_rejects_a_non_positive_interval(monkeypatch):
    """argparse's own exit: usage on stderr and a non-zero status, rather than
    a daemon that spins or dies on its first sleep."""
    from aiforge_core.memory.sync import loop

    started: list = []
    monkeypatch.setattr(loop, "run_forever", lambda i: started.append(i))

    for bad in ("0", "-5"):
        monkeypatch.setattr(sys, "argv", ["aiforge-sync", "--interval", bad])
        with pytest.raises(SystemExit) as exc:
            loop.main()
        assert exc.value.code == 2, bad

    assert started == [], "the daemon must not start on a rejected interval"


def test_a_permanently_failing_cycle_says_so(monkeypatch, tmp_path):
    """A cycle that fails once is weather; a cycle that fails every time is a
    full disk or an unparseable state file. Both logged the same line forever,
    so "has not synced since Tuesday" looked exactly like "syncing fine".

    Captured off ``loop._log`` rather than via ``caplog``: importing the API
    sets ``propagate = False`` on the ``aiforge`` logger, so whether caplog sees
    anything depends on which other test ran first. That made this assertion
    pass alone and fail in a full run.
    """
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.okf import tiers
    from aiforge_core.memory.sync import loop

    logged: list[str] = []

    def _boom():
        raise RuntimeError("disk is full")

    monkeypatch.setattr(loop, "run_once", _boom)
    monkeypatch.setattr(tiers, "run_after_sync", lambda *_a, **_k: None)
    monkeypatch.setattr(loop._log, "error",
                        lambda msg, *a, **_k: logged.append(msg % a if a else msg))
    _stop_at_sleep(monkeypatch, loop, after=loop.REPEATED_FAILURES)

    with pytest.raises(_Stop):
        loop.run_forever(interval=7)

    assert "continuing" in logged[0], "one bad cycle is still just a bad cycle"
    assert any("consecutive" in m for m in logged), (
        "a fault that never clears must be distinguishable from a bad cycle")


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
