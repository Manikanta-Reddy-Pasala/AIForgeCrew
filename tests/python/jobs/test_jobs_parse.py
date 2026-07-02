"""NL → job-draft parsing: one hermetic LLM call, croniter-gated.
Fail CLOSED at creation time — a bad job must never be born."""
from __future__ import annotations

import json

from aiforge_core.jobs import parse


def _fake(payload):
    def _complete(role, messages, **kw):
        _fake.role = role
        return payload
    return _complete


def test_parse_happy_path(monkeypatch):
    monkeypatch.setattr("aiforge_core.llm.client.complete", _fake(json.dumps({
        "name": "GitLab comments digest", "cron": "0 8 * * *",
        "ticket_title": "Pull GitLab comments (daily digest)",
        "ticket_body": "Fetch and summarize all new GitLab comments.",
        "project": None})))
    out = parse.parse_instructions("pull all gitlab comments every day at 8am")
    assert out["ok"] is True
    assert out["draft"]["cron"] == "0 8 * * *"
    assert out["human_schedule"] == "Every day at 08:00"
    assert len(out["next_runs"]) == 3
    assert _fake.role == "triage"


def test_parse_invalid_cron_fails_closed(monkeypatch):
    monkeypatch.setattr("aiforge_core.llm.client.complete", _fake(json.dumps({
        "name": "x", "cron": "99 99 * * *",
        "ticket_title": "t", "ticket_body": "b", "project": None})))
    out = parse.parse_instructions("do something weird")
    assert out["ok"] is False
    assert "cron" in out["error"].lower()


def test_parse_non_json_fails_closed(monkeypatch):
    monkeypatch.setattr("aiforge_core.llm.client.complete",
                        _fake("sorry, I can't do that"))
    out = parse.parse_instructions("anything")
    assert out["ok"] is False


def test_parse_missing_fields_fail_closed(monkeypatch):
    monkeypatch.setattr("aiforge_core.llm.client.complete", _fake(json.dumps({
        "name": "x", "cron": "0 8 * * *"})))   # no ticket_title/body
    out = parse.parse_instructions("anything")
    assert out["ok"] is False


def test_parse_llm_error_fails_closed(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("llm down")
    monkeypatch.setattr("aiforge_core.llm.client.complete", boom)
    out = parse.parse_instructions("anything")
    assert out["ok"] is False


def test_human_schedule_common_shapes():
    assert parse.human_schedule("0 8 * * *") == "Every day at 08:00"
    assert parse.human_schedule("45 9 * * 1-5") == "Weekdays at 09:45"
    assert parse.human_schedule("30 17 * * 5") == "Every Friday at 17:30"
    assert parse.human_schedule("*/15 * * * *") == "Every 15 minutes"
    # Anything unusual falls back to the raw expression.
    assert parse.human_schedule("0 8 1 * *") == "cron: 0 8 1 * *"


def test_parse_placeholder_echo_fails_closed(monkeypatch):
    import json as _json
    monkeypatch.setattr("aiforge_core.llm.client.complete", _fake(_json.dumps({
        "name": "x", "cron": "0 8 * * *",
        "ticket_title": "t", "ticket_body": "...", "project": None})))
    out = parse.parse_instructions("anything")
    assert out["ok"] is False
    assert "placeholder" in out["error"]


def test_human_schedule_sunday_aliases():
    assert parse.human_schedule("0 8 * * 0") == "Every Sunday at 08:00"
    assert parse.human_schedule("0 8 * * 7") == "Every Sunday at 08:00"


def test_next_runs_deterministic():
    from datetime import datetime
    runs = parse.next_runs("0 8 * * *", n=2,
                           base=datetime(2026, 7, 2, 12, 0, 0))
    assert runs == ["2026-07-03T08:00:00", "2026-07-04T08:00:00"]
