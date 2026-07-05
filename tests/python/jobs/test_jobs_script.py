"""Script jobs — local-folder storage, sandboxed exec, and the scheduler's
``kind=script`` fire branch (deterministic ops; no ticket / no LLM per tick)."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime

import pytest

from aiforge_core.jobs import scheduler, scripts, store

NOW = datetime(2026, 7, 2, 12, 0, 0)


@pytest.fixture(autouse=True)
def _tmp_env(monkeypatch, tmp_path):
    # jobs.db + the script folder both under the tmp config dir.
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_JOBS_DB_PATH", str(tmp_path / "jobs.db"))


# ── scripts module ───────────────────────────────────────────────────────

def test_write_script_lands_in_jobs_dir_executable():
    p = scripts.write_script("My Job!", "echo hi")
    assert scripts.is_within_jobs_dir(p)
    assert os.path.basename(p).startswith("my-job-")
    assert os.access(p, os.X_OK)
    with open(p) as fh:
        assert fh.read().startswith("#!")           # shebang prepended


def test_write_script_rejects_empty():
    with pytest.raises(ValueError):
        scripts.write_script("x", "   ")


def test_run_script_happy(tmp_path):
    marker = tmp_path / "ran.txt"
    p = scripts.write_script("marker", f"echo done > {marker}")
    res = scripts.run_script(p)
    assert res["ok"] is True
    assert res["returncode"] == 0
    assert marker.exists()


def test_run_script_nonzero_exit_is_not_ok():
    p = scripts.write_script("boom", "exit 3")
    res = scripts.run_script(p)
    assert res["ok"] is False
    assert res["returncode"] == 3
    assert "exited 3" in res["error"]


def test_run_script_refuses_path_outside_jobs_dir(tmp_path):
    evil = tmp_path / "evil.sh"
    evil.write_text("#!/usr/bin/env bash\necho pwned\n")
    res = scripts.run_script(str(evil))
    assert res["ok"] is False
    assert "outside jobs dir" in res["error"]


def test_run_script_missing_file():
    res = scripts.run_script(os.path.join(scripts.jobs_dir(), "nope.sh"))
    assert res["ok"] is False
    assert "not found" in res["error"]


def test_delete_script_removes_file():
    p = scripts.write_script("gone", "echo bye")
    assert os.path.isfile(p)
    assert scripts.delete_script(p) is True
    assert not os.path.exists(p)


def test_delete_script_missing_file_is_false():
    p = os.path.join(scripts.jobs_dir(), "nope.sh")
    assert scripts.delete_script(p) is False


def test_delete_script_refuses_path_outside_jobs_dir(tmp_path):
    evil = tmp_path / "evil.sh"
    evil.write_text("echo pwned\n")
    assert scripts.delete_script(str(evil)) is False
    assert evil.exists()  # refused, not deleted


def test_delete_script_empty_path_is_false():
    assert scripts.delete_script("") is False
    assert scripts.delete_script(None) is False


# ── store round-trip + migration ─────────────────────────────────────────

def test_store_round_trips_kind_and_script_path():
    j = store.create(name="s", cron="0 8 * * *", ticket_title="s",
                     ticket_body="runs", next_run_at="2026-07-02T08:00:00",
                     kind="script", script_path="/some/path.sh")
    got = store.get(j["id"])
    assert got["kind"] == "script"
    assert got["script_path"] == "/some/path.sh"


def test_ticket_jobs_default_to_kind_ticket():
    j = store.create(name="t", cron="0 8 * * *", ticket_title="t",
                     ticket_body="b", next_run_at="2026-07-02T08:00:00")
    assert store.get(j["id"])["kind"] == "ticket"


def test_migrates_legacy_jobs_db(monkeypatch, tmp_path):
    """A jobs.db created before the script columns gains them on next use."""
    db = tmp_path / "legacy.db"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT,"
        " cron TEXT, ticket_title TEXT, ticket_body TEXT, project TEXT,"
        " enabled INTEGER NOT NULL DEFAULT 1, last_run_at TEXT,"
        " next_run_at TEXT NOT NULL, last_error TEXT, created_at TEXT NOT NULL);")
    con.execute("INSERT INTO jobs (name, cron, ticket_title, ticket_body,"
                " next_run_at, created_at) VALUES ('old','0 8 * * *','t','b',"
                " '2026-07-02T08:00:00','2026-07-01T00:00:00')")
    con.commit()
    con.close()
    monkeypatch.setenv("AIFORGE_JOBS_DB_PATH", str(db))
    store.reset_backend_for_tests()
    # list + create must not crash and the legacy row reads kind='ticket'.
    rows = store.list_jobs()
    assert rows[0]["kind"] == "ticket"
    j = store.create(name="new", cron="0 8 * * *", ticket_title="t",
                     ticket_body="b", next_run_at="2026-07-02T08:00:00",
                     kind="script", script_path="/p.sh")
    assert store.get(j["id"])["kind"] == "script"
    store.reset_backend_for_tests()


# ── scheduler fire branch ────────────────────────────────────────────────

def _mk_script(**over):
    base = dict(name="run-thing", cron="0 8 * * *", ticket_title="run-thing",
                ticket_body="runs a script", next_run_at="2026-07-02T08:00:00",
                kind="script")
    base.update(over)
    return store.create(**base)


def test_fire_runs_script_and_advances(tmp_path):
    marker = tmp_path / "fired.txt"
    p = scripts.write_script("fire", f"echo ok > {marker}")
    j = _mk_script(script_path=p)
    assert scheduler.fire(j, now=NOW) is True
    assert marker.exists()
    got = store.get(j["id"])
    assert got["last_run_at"] == "2026-07-02T12:00:00"
    assert got["next_run_at"] == "2026-07-03T08:00:00"   # advanced from NOW
    assert got["last_error"] is None


def test_fire_failing_script_records_error_and_still_advances():
    p = scripts.write_script("failer", "echo boom >&2; exit 5")
    j = _mk_script(script_path=p)
    assert scheduler.fire(j, now=NOW) is False
    got = store.get(j["id"])
    assert got["last_error"] and "exited 5" in got["last_error"]
    assert got["next_run_at"] == "2026-07-03T08:00:00"   # advanced — no hot loop


def test_fire_script_creates_no_ticket(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr("aiforge_core.tickets.store.create",
                        lambda **k: called.__setitem__("n", called["n"] + 1))
    p = scripts.write_script("noticket", "true")
    scheduler.fire(_mk_script(script_path=p), now=NOW)
    assert called["n"] == 0        # script jobs never touch the ticket pipeline
