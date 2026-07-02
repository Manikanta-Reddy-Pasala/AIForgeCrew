"""jobs store backend SELECTION + SQLite round-trip.

Same shape as test_chat_store_backend: the PG impl needs a live Postgres to
fully verify; here we assert selection + the SQLite path still round-trips.
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def js(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_JOBS_DB_PATH", str(tmp_path / "jobs.db"))
    monkeypatch.delenv("AIFORGE_PG_URL", raising=False)
    import aiforge_core.jobs.store as m
    importlib.reload(m)
    from aiforge_core.config import env
    monkeypatch.setattr(env, "AIFORGE_USE_SQLITE", True, raising=False)
    monkeypatch.setattr(env, "AIFORGE_PG_URL", None, raising=False)
    m.reset_backend_for_tests()
    return m


def _mk(js, **over):
    base = dict(name="digest", cron="0 8 * * *",
                ticket_title="Pull comments", ticket_body="body",
                project=None, next_run_at="2026-07-03T08:00:00")
    base.update(over)
    return js.create(**base)


def test_sqlite_selected_when_no_pg(js):
    assert isinstance(js._backend(), js._SqliteJobStore)
    assert js._backend().name == "sqlite"


def test_pg_selected_when_pg_url_set(js, monkeypatch):
    from aiforge_core.config import env
    monkeypatch.setattr(env, "AIFORGE_USE_SQLITE", False, raising=False)
    monkeypatch.setattr(env, "AIFORGE_PG_URL",
                        "postgresql://u@127.0.0.1:5432/db", raising=False)
    js.reset_backend_for_tests()
    be = js._backend()
    assert isinstance(be, js._PgJobStore)
    assert be.name == "postgres"
    assert be.dsn == "postgresql://u@127.0.0.1:5432/db"


def test_sqlite_crud_roundtrip(js):
    j = _mk(js)
    assert j["id"] > 0
    assert j["enabled"] is True
    assert j["last_run_at"] is None
    got = js.get(j["id"])
    assert got["name"] == "digest"
    assert got["cron"] == "0 8 * * *"
    js.update(j["id"], enabled=False, last_error="boom")
    got = js.get(j["id"])
    assert got["enabled"] is False
    assert got["last_error"] == "boom"
    assert len(js.list_jobs()) == 1
    assert js.delete(j["id"]) is True
    assert js.get(j["id"]) is None


def test_sqlite_due_and_mark_fired(js):
    _mk(js, next_run_at="2020-01-01T00:00:00")
    due = js.due_jobs("2026-07-03T08:00:00")
    assert len(due) == 1
    jid = due[0]["id"]
    js.mark_fired(jid, last_run_at="2026-07-03T08:00:00",
                  next_run_at="2026-07-04T08:00:00")
    got = js.get(jid)
    assert got["last_run_at"] == "2026-07-03T08:00:00"
    assert got["next_run_at"] == "2026-07-04T08:00:00"
    # no longer due at a time before the new next_run_at
    assert js.due_jobs("2026-07-03T09:00:00") == []
