"""Self-editing block tool (#8) + user-preference (#9) gating."""
from __future__ import annotations

from aiforge_core.runtime import memory_block_tool as mb
from aiforge_core.runtime import user_prefs as up


def test_block_tool_needs_repo(monkeypatch):
    monkeypatch.delenv("AIFORGE_AFM_REPO", raising=False)
    r = mb.memory_block(action="read")
    assert r["ok"] is False
    assert "repo" in r["error"]


def test_block_tool_no_driver_embedded(monkeypatch):
    monkeypatch.setenv("AIFORGE_AFM_REPO", "x")
    import aiforge_core.runtime.learner_persist as lp
    monkeypatch.setattr(lp, "_open_driver", lambda: None)
    r = mb.memory_block(action="read")
    assert r["ok"] is False
    assert "driver" in r["error"]


def test_user_prefs_block_empty_when_no_driver(monkeypatch):
    monkeypatch.setattr(up, "_driver", lambda: None)
    assert up.get_preferences() == ""
    assert up.preferences_block() == ""
    assert up.record_preference("x")["ok"] is False


def test_user_prefs_block_renders_when_present(monkeypatch):
    monkeypatch.setattr(up, "get_preferences", lambda: "always use yarn")
    b = up.preferences_block()
    assert "USER PREFERENCES" in b
    assert "always use yarn" in b
