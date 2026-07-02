"""Stale-in_progress reaper: a hard-crashed runner leaves a ticket stuck
``in_progress`` forever (re-claim only selects ``todo``). The reaper resets
rows whose ``claimed_at`` is older than the lease back to ``todo`` and bumps
``reclaim_count`` (the previously-dead dashboard metric)."""
import importlib

import pytest


@pytest.fixture
def store(monkeypatch, tmp_path):
    # Force the embedded SQLite backend hermetically — an earlier full-suite
    # test may have left AIFORGE_PG_URL/FORCE_PG set, which env.py captures at
    # IMPORT into AIFORGE_USE_SQLITE, so we must clear them AND reload env
    # before backend_factory reads it (else get_backend picks an unreachable PG).
    monkeypatch.delenv("AIFORGE_PG_URL", raising=False)
    monkeypatch.delenv("AIFORGE_DSN", raising=False)
    monkeypatch.delenv("AIFORGE_FORCE_PG", raising=False)
    monkeypatch.delenv("AIFORGE_TICKET_LEASE_S", raising=False)
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


def _backend():
    from aiforge_core.tickets import backend_factory
    return backend_factory.get_backend()


def _backdate_claimed_at(ticket_id, seconds_ago):
    """Force a ticket's claimed_at into the past to simulate a stale claim."""
    be = _backend()
    with be._conn() as c:
        c.execute(
            "UPDATE tickets SET claimed_at = "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now',?) WHERE id = ?",
            (f"-{int(seconds_ago)} seconds", ticket_id),
        )


def test_reap_resets_stale_in_progress_and_bumps_reclaim(store):
    t = store.create(title="stale one", body="x")
    claimed = store.claim_next_any()
    assert claimed is not None and claimed.status == "in_progress"
    _backdate_claimed_at(claimed.id, 7200)   # 2h old, lease default 3600s

    reset = store.reap_stale_in_progress(3600)
    # returns the reset ids
    assert claimed.id in list(reset)

    again = store.get(t.identifier)
    assert again.status == "todo"
    assert int(again.metadata.get("reclaim_count") or 0) == 1

    # re-claimable now (was NOT before the reaper)
    reclaimed = store.claim_next_any()
    assert reclaimed is not None and reclaimed.id == claimed.id


def test_reap_leaves_fresh_in_progress_alone(store):
    store.create(title="fresh one", body="x")
    claimed = store.claim_next_any()          # claimed_at = now
    assert claimed.status == "in_progress"

    reset = store.reap_stale_in_progress(3600)
    assert claimed.id not in list(reset)
    assert store.get(claimed.identifier).status == "in_progress"


def test_reap_ignores_todo_and_done(store):
    todo = store.create(title="todo one", body="x")
    done = store.create(title="done one", body="x")
    d = store.claim_next_any()   # claims todo-one first (older); mark it done
    # claim_next_any claims oldest → 'todo one'; move it to done
    store.update_status(d.id, "done")
    _backdate_claimed_at(d.id, 99999)         # even if old, done must be ignored

    reset = list(store.reap_stale_in_progress(1))
    assert todo.id not in reset   # todo one is now 'done'
    assert done.id not in reset   # still 'todo'
    assert store.get(done.identifier).status == "todo"
    assert store.get(d.identifier).status == "done"


def test_reap_env_default_lease(store, monkeypatch):
    monkeypatch.setenv("AIFORGE_TICKET_LEASE_S", "10")
    store.create(title="lease one", body="x")
    claimed = store.claim_next_any()
    _backdate_claimed_at(claimed.id, 60)      # older than the 10s env lease
    reset = list(store.reap_stale_in_progress())   # no arg → env default
    assert claimed.id in reset
