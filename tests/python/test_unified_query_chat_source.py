"""F3: chat sessions surface as a unified_query source.

Chat message content lived in its own SQLite silo (chat_store) that
unified_query never read, so the team pipeline never saw what was worked
out in chat. Add it as a guarded, soft-failing source tagged "chat".
"""
import pytest

from aiforge_core.memory import unified_query as uq


@pytest.fixture(autouse=True)
def _no_other_sources(monkeypatch):
    # Keep the test hermetic: embedded off + no afm repo so only the chat
    # source (and cheap no-op MCP calls that soft-fail) contribute.
    monkeypatch.setattr(
        "aiforge_core.memory.backend_select.embedded", lambda: False,
    )
    for k in ("AIFORGE_AFM_REPO",):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AIFORGE_UMEM_CHAT", "1")


def test_chat_source_included(monkeypatch):
    def _fake_search(text, **kwargs):
        return [
            {"session_id": 3, "session_title": "sync work",
             "role": "assistant", "content": "we decided to poll every 30s",
             "created_at": "t"},
            {"session_id": 3, "session_title": "sync work",
             "role": "user", "content": "why 30s not 10s",
             "created_at": "t"},
        ]

    monkeypatch.setattr(
        "aiforge_core.runtime.chat_store.search_messages", _fake_search,
    )
    res = uq.query("polling interval decision", limit=10)
    chat_hits = [h for h in res["hits"] if h.get("source") == "chat"]
    assert len(chat_hits) == 2
    assert "chat" in res["used_sources"]
    assert any("poll every 30s" in (h.get("text") or "") for h in chat_hits)


def test_chat_source_soft_fails(monkeypatch):
    def _boom(text, **kwargs):
        raise RuntimeError("chat db locked")

    monkeypatch.setattr(
        "aiforge_core.runtime.chat_store.search_messages", _boom,
    )
    # must not crash — other sources (none here) still return cleanly
    res = uq.query("anything", limit=5)
    assert isinstance(res["hits"], list)
    assert any("chat" in e for e in res["errors"])


def test_chat_source_gated_off(monkeypatch):
    monkeypatch.setenv("AIFORGE_UMEM_CHAT", "0")
    called = {"n": 0}

    def _fake_search(text, **kwargs):
        called["n"] += 1
        return [{"session_id": 1, "content": "x", "role": "user",
                 "created_at": "t"}]

    monkeypatch.setattr(
        "aiforge_core.runtime.chat_store.search_messages", _fake_search,
    )
    res = uq.query("q", limit=5)
    assert called["n"] == 0
    assert "chat" not in res["used_sources"]
