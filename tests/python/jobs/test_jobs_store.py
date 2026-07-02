"""Jobs store — CRUD + due-query semantics against an isolated tmp DB."""
from __future__ import annotations

import pytest

from aiforge_core.jobs import store


@pytest.fixture(autouse=True)
def _tmp_db(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_JOBS_DB_PATH", str(tmp_path / "jobs.db"))


def _mk(**over):
    base = dict(name="digest", cron="0 8 * * *",
                ticket_title="Pull GitLab comments",
                ticket_body="Fetch and summarize all new GitLab comments.",
                project=None, next_run_at="2026-07-03T08:00:00")
    base.update(over)
    return store.create(**base)


def test_create_and_get_roundtrip():
    j = _mk()
    assert j["id"] > 0
    assert j["enabled"] is True
    assert j["last_run_at"] is None
    got = store.get(j["id"])
    assert got["name"] == "digest"
    assert got["cron"] == "0 8 * * *"


def test_list_jobs_returns_all():
    _mk(name="a")
    _mk(name="b")
    assert [j["name"] for j in store.list_jobs()] == ["a", "b"]


def test_update_whitelisted_fields():
    j = _mk()
    out = store.update(j["id"], name="renamed", enabled=False)
    assert out["name"] == "renamed"
    assert out["enabled"] is False


def test_update_unknown_field_rejected():
    j = _mk()
    with pytest.raises(ValueError):
        store.update(j["id"], nonsense="x")


def test_delete():
    j = _mk()
    assert store.delete(j["id"]) is True
    assert store.get(j["id"]) is None
    assert store.delete(j["id"]) is False


def test_due_jobs_semantics():
    past = _mk(name="past", next_run_at="2026-07-01T08:00:00")
    _mk(name="future", next_run_at="2099-01-01T08:00:00")
    paused = _mk(name="paused", next_run_at="2026-07-01T08:00:00")
    store.update(paused["id"], enabled=False)
    due = store.due_jobs("2026-07-02T00:00:00")
    assert [j["name"] for j in due] == ["past"]
    assert due[0]["id"] == past["id"]


def test_due_jobs_boundary_equality_is_due():
    j = _mk(next_run_at="2026-07-02T00:00:00")
    due = store.due_jobs("2026-07-02T00:00:00")
    assert [x["id"] for x in due] == [j["id"]]


def test_mark_fired_success_clears_error():
    j = _mk()
    store.update(j["id"], last_error="old boom")
    store.mark_fired(j["id"], last_run_at="2026-07-03T08:00:01",
                     next_run_at="2026-07-04T08:00:00")
    got = store.get(j["id"])
    assert got["last_run_at"] == "2026-07-03T08:00:01"
    assert got["next_run_at"] == "2026-07-04T08:00:00"
    assert got["last_error"] is None


def test_mark_fired_failure_records_error():
    j = _mk()
    store.mark_fired(j["id"], last_run_at="2026-07-03T08:00:01",
                     next_run_at="2026-07-04T08:00:00", last_error="boom")
    assert store.get(j["id"])["last_error"] == "boom"
