"""Agent jobs — the scheduler's ``kind=agent`` fire branch runs the request
through the chat agent (full jira/confluence/email tools, no code pipeline) on a
daemon thread and records the outcome on ``last_error``."""
from __future__ import annotations

import time
from datetime import datetime

import pytest

from aiforge_core.jobs import scheduler, store

NOW = datetime(2026, 7, 2, 12, 0, 0)


def _wait_until(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


@pytest.fixture(autouse=True)
def _tmp_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    store._conn.cache_clear() if hasattr(store, "_conn") else None
    yield


def _make_agent_job(body="read JIRA-1 and email me a summary"):
    return store.create(
        name="daily-jira-digest", cron="0 9 * * *",
        ticket_title="jira digest", ticket_body=body,
        next_run_at=NOW.isoformat(timespec="seconds"), kind="agent")


def test_agent_job_runs_chat_agent_and_marks_ok(monkeypatch):
    captured = {}

    def fake_run_chat_agent(messages, *, cwd, role, session_id):
        captured["prompt"] = messages[0]["content"]
        captured["role"] = role
        captured["session_id"] = session_id
        yield {"type": "tool", "name": "jira_read", "result": {"ok": True}}
        yield {"type": "message", "text": "Sent the digest email."}

    monkeypatch.setattr("aiforge_core.runtime.chat_agent.run_chat_agent",
                        fake_run_chat_agent)
    job = _make_agent_job()
    assert scheduler._fire_agent(job) is True         # dispatched
    # Wait on the AGENT having run, not on last_error being None — that is
    # already true before the background thread starts, so under load the
    # assertions below raced the dispatch (flaky in full-suite runs).
    assert _wait_until(lambda: "prompt" in captured)
    assert store.get(job["id"]).get("last_error") is None
    assert captured["prompt"] == "read JIRA-1 and email me a summary"
    assert captured["role"] == "chat"          # chat agent, full tools
    assert captured["session_id"] is None      # autonomous


def test_agent_job_records_error(monkeypatch):
    def fake_run(messages, *, cwd, role, session_id):
        yield {"type": "error", "text": "jira_not_configured"}

    monkeypatch.setattr("aiforge_core.runtime.chat_agent.run_chat_agent", fake_run)
    job = _make_agent_job()
    scheduler._fire_agent(job)
    assert _wait_until(
        lambda: (store.get(job["id"]).get("last_error") or "") == "jira_not_configured")


def test_fire_dispatches_agent_kind(monkeypatch):
    called = {}

    def _fake_fire_agent(job):
        called["id"] = job["id"]
        return True
    monkeypatch.setattr(scheduler, "_fire_agent", _fake_fire_agent)
    job = _make_agent_job()
    # fire advances the schedule then dispatches by kind
    assert scheduler.fire(job, now=NOW) is True
    assert called.get("id") == job["id"]
