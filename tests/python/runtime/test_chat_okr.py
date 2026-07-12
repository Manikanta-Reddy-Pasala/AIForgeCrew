"""T4 — session-end OKR compaction.

At session end (idle / explicit) a session's transcript is distilled by the
learner LLM into atomic durable items (decisions, learnings, meaningful user
inputs — NOT chit-chat), each routed to its scope (global / project / topic) via
classify_scope and folded into the matching OKR brief through md_store.capture.
"""
from __future__ import annotations

import types

import pytest


@pytest.fixture()
def mem(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    return tmp_path


_MSGS = [
    {"role": "user", "content": "thanks! also always run tests before commit"},
    {"role": "assistant", "content": "will do"},
    {"role": "user", "content": "OrderController maps /orders in svc"},
]


def test_compact_session_routes_items_to_scoped_briefs(monkeypatch, mem):
    monkeypatch.setenv("AIFORGE_OKR_SCOPE_LLM", "1")
    from aiforge_core.memory import md_store
    from aiforge_core.runtime import chat_okr

    monkeypatch.setattr("aiforge_core.runtime.chat_store.get_messages",
                        lambda sid: _MSGS)

    def _fake(role, messages, model, *a, **k):
        n = getattr(model, "__name__", "")
        if n == "ScopeDecision":
            c = messages[-1]["content"]
            scope = "global" if "tests" in c else "project"
            return types.SimpleNamespace(scope=scope, repo="", topic="")
        # extraction
        return types.SimpleNamespace(items=[
            types.SimpleNamespace(text="always run tests before commit",
                                  kind="learning"),
            types.SimpleNamespace(text="OrderController maps /orders",
                                  kind="project_learning")])

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _fake)
    res = chat_okr.compact_session("s1", repo="svc")
    assert res["ok"] and res["captured"] == 2
    # global item promoted to shared, project item under its repo
    assert (md_store.memory_dir() / "compacted-shared.md").exists()
    assert (md_store.memory_dir() / "compacted-svc.md").exists()


def test_compact_session_skips_short(monkeypatch, mem):
    from aiforge_core.runtime import chat_okr
    monkeypatch.setattr("aiforge_core.runtime.chat_store.get_messages",
                        lambda sid: [{"role": "user", "content": "hi"}])
    res = chat_okr.compact_session("s1", repo="svc", min_turns=4)
    assert res["ok"] and res.get("skipped") == "too_short"


def test_compact_session_disabled(monkeypatch, mem):
    monkeypatch.setenv("AIFORGE_SESSION_COMPACT", "off")
    from aiforge_core.runtime import chat_okr
    res = chat_okr.compact_session("s1", repo="svc")
    assert res["skipped"] == "disabled"


def test_compact_session_soft_fails_on_llm_error(monkeypatch, mem):
    from aiforge_core.runtime import chat_okr
    monkeypatch.setattr("aiforge_core.runtime.chat_store.get_messages",
                        lambda sid: _MSGS)

    def _boom(*a, **k):
        raise RuntimeError("model down")

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _boom)
    res = chat_okr.compact_session("s1", repo="svc")
    assert res["ok"] and res["captured"] == 0


def test_previous_session_brief_returns_prior(monkeypatch, mem):
    from aiforge_core.runtime import chat_okr
    sessions = [{"id": 5, "cwd": None}, {"id": 4, "cwd": None}]  # newest first
    monkeypatch.setattr("aiforge_core.runtime.chat_store.list_sessions",
                        lambda: sessions)
    monkeypatch.setattr(
        "aiforge_core.runtime.chat_store.get_messages",
        lambda sid: ([{"role": "user", "content": "we chose the SQLite backend"},
                      {"role": "assistant", "content": "done"}]
                     if sid == 4 else []))
    out = chat_okr.previous_session_brief(5)
    assert "SQLite" in out and "4" in out


def test_previous_session_brief_empty_when_only_current(monkeypatch, mem):
    from aiforge_core.runtime import chat_okr
    monkeypatch.setattr("aiforge_core.runtime.chat_store.list_sessions",
                        lambda: [{"id": 5}])
    assert chat_okr.previous_session_brief(5) == ""
