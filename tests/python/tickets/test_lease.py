"""One run per ticket: claim heartbeat, reclaim cap, specific claims, worktree lock.

Reproduced on nuc by the 2026-09-11 orchestration review: the claim lived 1h,
the pipeline may run 90 min, nothing renewed it — another runner's reaper
requeued the LIVE ticket, a second runner claimed it and reset the shared
worktree, wiping the first run's edits.
"""
import importlib
import time

import pytest


@pytest.fixture
def store(monkeypatch, tmp_path):
    """Hermetic embedded-SQLite store (same recipe as test_reaper)."""
    for var in ("AIFORGE_PG_URL", "AIFORGE_DSN", "AIFORGE_FORCE_PG",
                "AIFORGE_TICKET_LEASE_S", "AIFORGE_TICKET_MAX_RECLAIMS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_TICKETS_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_DB_PATH", str(tmp_path / "t.db"))
    import aiforge_core.config.env as envmod
    importlib.reload(envmod)
    from aiforge_core.tickets import backend_factory
    importlib.reload(backend_factory)
    backend_factory.reset_backend_for_tests()
    from aiforge_core.tickets import store as s
    importlib.reload(s)
    yield s
    backend_factory.reset_backend_for_tests()


def _backdate_claimed_at(ticket_id, seconds_ago):
    from aiforge_core.tickets import backend_factory
    with backend_factory.get_backend()._conn() as c:
        c.execute("UPDATE tickets SET claimed_at = "
                  "strftime('%Y-%m-%dT%H:%M:%fZ','now',?) WHERE id = ?",
                  (f"-{int(seconds_ago)} seconds", ticket_id))


def _row(tid):
    from aiforge_core.tickets import backend_factory
    return backend_factory.get_backend().fetch_ticket(tid)


def test_a_live_run_keeps_its_claim_and_is_never_reaped(store):
    from aiforge_core.tickets import lease
    importlib.reload(lease)
    store.create(title="long run", body="x")
    t = store.claim_next_any()
    _backdate_claimed_at(t.id, 7200)             # would be reaped as stale…
    with lease.hold_claim(t.id, interval_s=0.05):
        time.sleep(0.3)                          # …but the heartbeat renews it
        assert store.reap_stale_in_progress(3600) == []
    assert _row(t.id)["status"] == "in_progress"


def test_a_crashed_run_stops_renewing_and_is_reaped(store):
    store.create(title="crashed", body="x")
    t = store.claim_next_any()
    _backdate_claimed_at(t.id, 7200)
    assert t.id in store.reap_stale_in_progress(3600)
    assert _row(t.id)["status"] == "todo"


def test_a_ticket_that_keeps_dying_is_blocked_not_requeued_forever(store, monkeypatch):
    monkeypatch.setenv("AIFORGE_TICKET_MAX_RECLAIMS", "2")
    store.create(title="poison", body="x")
    for expected in ("todo", "todo", "blocked"):
        t = store.claim_next_any()
        assert t is not None
        _backdate_claimed_at(t.id, 7200)
        store.reap_stale_in_progress(3600)
        row = _row(t.id)
        assert row["status"] == expected
    assert "keeps dying" in row["metadata"]["blocked_reason"]


def test_a_specific_claim_refuses_a_running_ticket(store):
    t = store.create(title="p", body="x")
    assert store.claim_ticket(t.id) is not None          # operator run takes it
    assert store.claim_next_any() is None                # the runner cannot
    assert store.claim_ticket(t.id) is None              # nor a second operator run


def test_a_deferred_ticket_is_skipped_until_its_retry_after(store):
    from aiforge_core.tickets import lease
    importlib.reload(lease)
    a = store.create(title="waits for its sibling", body="x")
    b = store.create(title="other work", body="y")
    first = store.claim_next_any()
    assert first.id == a.id
    lease.defer(a.id, seconds=60, reason="worktree busy")
    nxt = store.claim_next_any()
    assert nxt is not None and nxt.id == b.id            # not starved behind a


def test_the_worktree_lock_admits_one_run_at_a_time(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    from aiforge_core.tickets import lease
    with lease.worktree_lock("ONE-7") as first:
        assert first is True
        with lease.worktree_lock("ONE-7") as second:
            assert second is False                        # the sibling backs off
        with lease.worktree_lock("ONE-8") as other:
            assert other is True                          # other roots unaffected
    with lease.worktree_lock("ONE-7") as again:
        assert again is True                              # released on exit
