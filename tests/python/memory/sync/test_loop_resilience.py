"""What the daemon must outlive.

Every case here killed the process or silently ate the cycle's result row. A
sync daemon that exits is worse than one that syncs nothing: the supervisor
restarts it into the same state thirty seconds later, forever, and knowledge
compaction rides this same loop, so it stops too — from a cause no log connects
to the thing that broke.
"""
from __future__ import annotations

import json
import sys

import pytest


def _reachable(monkeypatch):
    """Make the admin answer, so "was it contacted" is observable in the row."""
    from aiforge_core.memory.sync import transport

    monkeypatch.setattr(transport, "fetch_manifest",
                        lambda *_a, **_k: {"manifest": [], "admin": "hub"})
    monkeypatch.setattr(transport, "fetch_groups", lambda *_a, **_k: [])
    monkeypatch.setattr(transport, "offer", lambda *_a, **_k: [])


def _env(monkeypatch, tmp_path, peer_id: str = "book", admin="http://stub"):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", peer_id)
    monkeypatch.delenv("AIFORGE_ROLE", raising=False)
    if admin:
        monkeypatch.setenv("AIFORGE_ADMIN_URL", admin)
    else:
        monkeypatch.delenv("AIFORGE_ADMIN_URL", raising=False)


# ── 1. state the daemon reads must not be able to kill it ─────────────────

def test_a_garbage_role_does_not_end_the_cycle(monkeypatch, tmp_path):
    """``AIFORGE_ROLE`` is read on every cycle and inside the compaction gate.
    A typo must be ignored, not fatal — this daemon is built to outlive bad
    state, not to be ended by a misspelled word."""
    _env(monkeypatch, tmp_path)
    _reachable(monkeypatch)
    from aiforge_core.memory.sync import loop

    for bad in ("Admin ", "leader", "1", "spoke;rm -rf /"):
        monkeypatch.setenv("AIFORGE_ROLE", bad)
        assert isinstance(loop.run_once(), list), bad


def test_an_unreadable_admin_id_cache_does_not_end_the_cycle(monkeypatch, tmp_path):
    """``admin.json`` is a plain file an operator can edit or a crash can
    truncate. Reading it must degrade to "we do not know the admin yet"."""
    _env(monkeypatch, tmp_path)
    _reachable(monkeypatch)
    from aiforge_core.memory.sync import loop

    cfg = tmp_path / "cfg"
    cfg.mkdir(parents=True, exist_ok=True)
    for junk in ("{", "[]", "null", '{"id": ["a"]}'):
        (cfg / "admin.json").write_text(junk, encoding="utf-8")
        rows = loop.run_once()
        assert len(rows) == 1, junk
        assert rows[0]["ok"] is True, junk


def test_a_push_that_raises_does_not_cost_the_pull(monkeypatch, tmp_path):
    """Push runs first. Before it was wrapped, a raising offer took the whole
    cycle with it — including the fold that rides the same loop."""
    _env(monkeypatch, tmp_path)
    _reachable(monkeypatch)
    from aiforge_core.memory.sync import loop, transport

    monkeypatch.setattr(transport, "fetch_groups", lambda *_a, **_k: [])
    monkeypatch.setattr(transport, "offer",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError(28, "No space")))

    rows = loop.run_once()

    assert len(rows) == 1
    assert rows[0]["ok"] is True, "a failed push ate the pull"


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

def test_a_wrong_shaped_manifest_response_is_survived(monkeypatch, tmp_path):
    """An admin answering `{"manifest": {"not":"a list"}}` — a different build,
    a proxy error page — blew up mid-cycle and the result row vanished from the
    cycle output entirely, so nothing recorded that the admin had been tried."""
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import loop, transport

    body = json.dumps({"manifest": {"not": "a list"}, "admin": "hub"}).encode()
    monkeypatch.setattr(transport, "_fetch", lambda *_a, **_k: body)
    monkeypatch.setattr(transport, "fetch_groups", lambda *_a, **_k: [])
    monkeypatch.setattr(transport, "offer", lambda *_a, **_k: [])

    rows = loop.run_once()

    assert len(rows) == 1
    assert rows[0]["admin"] == "http://stub"
    assert rows[0]["ok"] is True


def test_the_row_survives_a_pull_that_fails_part_way(monkeypatch, tmp_path):
    """Under a global ENOSPC the entries were correctly counted as rejected and
    then the bookkeeping raised, throwing the whole row away: the cycle reported
    `[]` and one WARNING was the only evidence it had run at all."""
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import loop, transport

    monkeypatch.setattr(transport, "fetch_manifest",
                        lambda *_a, **_k: {"manifest": [
                            {"kind": "A", "path": "captures/a.md", "hash": "ab"}],
                            "admin": "hub"})
    monkeypatch.setattr(transport, "fetch_blob", lambda *_a, **_k: None)
    monkeypatch.setattr(transport, "fetch_groups", lambda *_a, **_k: [])
    monkeypatch.setattr(transport, "offer", lambda *_a, **_k: [])

    rows = loop.run_once()

    assert len(rows) == 1
    assert rows[0]["rejected"] == 1, "counts earned before the failure are kept"
