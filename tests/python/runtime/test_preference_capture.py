"""Auto-capture user preferences → upsert into GLOBAL memory (map to an existing
subject, replace-in-place) so the user never restates a default/convention.
"""
from __future__ import annotations

import tempfile

import pytest


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", tempfile.mkdtemp())
    return None


def test_upsert_replaces_same_subject(cfg):
    from aiforge_core.memory import sqlite_memory as m
    m.upsert_by_tag(text="default project is ENG", tag="pref:proj",
                    kind="preference", tags=["preference"])
    m.upsert_by_tag(text="default project is OPS", tag="pref:proj",
                    kind="preference", tags=["preference"])
    texts = [r["text"] for r in m.recall("default project", limit=10)]
    assert texts == ["default project is OPS"]        # replaced, not appended
    assert m.delete_by_tag("pref:proj") == 1
    assert m.recall("default project", limit=10) == []


def test_preferences_context_injected(cfg, monkeypatch):
    from aiforge_core.memory import sqlite_memory as m
    monkeypatch.setattr(
        "aiforge_core.memory.backend_select.embedded", lambda: True)
    m.upsert_by_tag(text="use tabs not spaces", tag="pref:indent",
                    kind="preference", tags=["preference"])
    from aiforge_core.runtime.chat_agent import _preferences_context
    ctx = _preferences_context(".")
    assert "USER PREFERENCES" in ctx
    assert "use tabs not spaces" in ctx


def test_capture_maps_and_upserts(cfg, monkeypatch):
    monkeypatch.setattr(
        "aiforge_core.memory.backend_select.embedded", lambda: True)
    # Stub the LLM to map the message to a subject + value.
    def _fake_complete(role, messages, **kw):
        return ('{"is_preference": true, "subject": "jira-default-project", '
                '"value": "default Jira project is ENG", "global": true}')
    monkeypatch.setattr("aiforge_core.llm.client.complete", _fake_complete)

    from aiforge_core.runtime import preference_capture as pc
    r = pc.capture("from now on use ENG as the default jira project", repo="x")
    assert r["ok"]
    assert r["captured"]
    assert r["subject"] == "jira-default-project"

    # Restatement with a new value maps to the SAME subject → replaces.
    def _fake2(role, messages, **kw):
        return ('{"is_preference": true, "subject": "jira-default-project", '
                '"value": "default Jira project is OPS", "global": true}')
    monkeypatch.setattr("aiforge_core.llm.client.complete", _fake2)
    pc.capture("actually default jira project should be OPS", repo="x")

    from aiforge_core.memory import sqlite_memory as m
    texts = [r["text"] for r in m.recall("jira project", limit=10)]
    assert texts == ["default Jira project is OPS"]


def test_gate_skips_ordinary_questions(cfg, monkeypatch):
    monkeypatch.setattr(
        "aiforge_core.memory.backend_select.embedded", lambda: True)
    called = {"n": 0}
    monkeypatch.setattr("aiforge_core.llm.client.complete",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or "{}")
    from aiforge_core.runtime import preference_capture as pc
    r = pc.capture("what does this function do?")
    assert not r["ok"]
    assert r["skipped"] == "no-gate"
    assert called["n"] == 0                    # LLM never hit for a plain question
