"""N4 — a timed-out tick must NOT release the per-repo lock while its
worker thread is still running (zombie double-ingest)."""
from __future__ import annotations

import os
import threading

import pytest

import aiforge_memory.features.delta.extract as delta
from aiforge_memory.features.flow.runner import IngestResult
from aiforge_memory.features.scheduler import runner as scheduler


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(os.path, "expanduser",
                        lambda p: p.replace("~", str(tmp_path)))
    # Tests share module state — start clean.
    scheduler._LIVE_WORKERS.clear()
    yield tmp_path
    scheduler._LIVE_WORKERS.clear()


def _schedule(tmp_path, timeout_seconds=0):
    return scheduler.RepoSchedule(
        name="zr", path=str(tmp_path), interval_seconds=600,
        timeout_seconds=timeout_seconds,
    )


def test_timeout_keeps_lock_and_next_tick_skips(home, monkeypatch):
    release = threading.Event()

    monkeypatch.setattr(scheduler, "fetch_and_maybe_pull",
                        lambda path, do_pull=True: scheduler.FetchOutcome(
                            True, False, 0))
    monkeypatch.setattr(delta, "ingest_delta",
                        lambda **kw: release.wait(timeout=30))

    rs = _schedule(home, timeout_seconds=0)   # join(0) → instant timeout
    log_lines: list[str] = []
    st1 = scheduler.tick_repo(rs, driver=None, state_conn=None,
                              log=log_lines.append)
    try:
        assert st1.last_status == "timeout"
        # Lock must still be held by the zombie.
        assert scheduler._lockfile("zr").exists()
        assert "zr" in scheduler._LIVE_WORKERS

        # Next tick: worker alive → skip, no double ingest, lock intact.
        st2 = scheduler.tick_repo(rs, driver=None, state_conn=None,
                                  log=log_lines.append)
        assert st2.last_status == "still_running"
        assert scheduler._lockfile("zr").exists()
    finally:
        release.set()

    scheduler._LIVE_WORKERS["zr"].join(timeout=30)

    # Worker observed dead → tick proceeds normally and lock cycles.
    monkeypatch.setattr(delta, "ingest_delta",
                        lambda **kw: IngestResult(status="no_changes",
                                                  pack_sha="", repo="zr"))
    rs_fast = _schedule(home, timeout_seconds=30)
    st3 = scheduler.tick_repo(rs_fast, driver=None, state_conn=None,
                              log=log_lines.append)
    assert st3.last_status == "no_changes"
    assert "zr" not in scheduler._LIVE_WORKERS
    assert not scheduler._lockfile("zr").exists()


def test_normal_tick_releases_lock(home, monkeypatch):
    monkeypatch.setattr(scheduler, "fetch_and_maybe_pull",
                        lambda path, do_pull=True: scheduler.FetchOutcome(
                            True, False, 0))
    monkeypatch.setattr(delta, "ingest_delta",
                        lambda **kw: IngestResult(status="no_changes",
                                                  pack_sha="", repo="zr"))
    rs = _schedule(home, timeout_seconds=30)
    st = scheduler.tick_repo(rs, driver=None, state_conn=None,
                             log=lambda m: None)
    assert st.last_status == "no_changes"
    assert not scheduler._lockfile("zr").exists()
    assert "zr" not in scheduler._LIVE_WORKERS
