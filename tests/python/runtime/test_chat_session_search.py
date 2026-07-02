"""Chat-session search + recall: prior conversations become searchable BEFORE
the agent answers, so simple chat draws on what it discussed in OTHER sessions.

Local + cheap: one SQLite query, no LLM, no network. Soft-fail everywhere."""
import importlib

import pytest


@pytest.fixture
def store(monkeypatch, tmp_path):
    """A fresh, isolated chat DB per test."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_CHAT_DB_PATH", str(tmp_path / "chat.db"))
    import aiforge_core.runtime.chat_store as cs
    importlib.reload(cs)
    return cs


def _seed(cs):
    """Three sessions with distinct topics."""
    s_kafka = cs.create_session(title="Kafka retries")["id"]
    cs.add_message(s_kafka, "user", "how do we handle kafka consumer retries?")
    cs.add_message(s_kafka, "assistant",
                   "Use a dead-letter topic and exponential backoff for kafka retries.")

    s_redis = cs.create_session(title="Redis cache")["id"]
    cs.add_message(s_redis, "user", "what redis eviction policy should we use?")
    cs.add_message(s_redis, "assistant", "allkeys-lru works well for a cache.")

    s_cur = cs.create_session(title="Current chat")["id"]
    cs.add_message(s_cur, "user", "unrelated current-session message")
    return s_kafka, s_redis, s_cur


# ── search_messages ────────────────────────────────────────────────────────

def test_search_finds_matching_messages(store):
    s_kafka, _s_redis, _s_cur = _seed(store)
    hits = store.search_messages("kafka retries")
    assert hits, "expected matches for kafka retries"
    assert any("kafka" in h["content"].lower() for h in hits)
    assert all(h["session_id"] == s_kafka for h in hits
               if "kafka" in h["content"].lower())
    # Session title travels with the hit.
    assert any(h["session_title"] == "Kafka retries" for h in hits)


def test_search_ranks_by_term_overlap(store):
    store.create_session(title="one term")
    s2 = store.create_session(title="two terms")["id"]
    s1 = store.create_session(title="single")["id"]
    store.add_message(s1, "user", "alpha only here")
    store.add_message(s2, "user", "alpha beta both here")
    hits = store.search_messages("alpha beta")
    assert len(hits) >= 2
    # The message matching BOTH tokens ranks first.
    assert hits[0]["session_id"] == s2


def test_search_excludes_session(store):
    _s_kafka, _s_redis, s_cur = _seed(store)
    store.add_message(s_cur, "user", "kafka question in the current session")
    hits = store.search_messages("kafka", exclude_session=s_cur)
    assert hits
    assert all(h["session_id"] != s_cur for h in hits)


def test_search_respects_limit(store):
    sid = store.create_session(title="many")["id"]
    for i in range(10):
        store.add_message(sid, "user", f"widget number {i}")
    hits = store.search_messages("widget", limit=3)
    assert len(hits) == 3


def test_search_empty_query_returns_empty(store):
    _seed(store)
    assert store.search_messages("") == []
    assert store.search_messages("  a  ") == []   # all tokens < 3 chars


def test_search_no_match_returns_empty(store):
    _seed(store)
    assert store.search_messages("zzzznonexistentterm") == []


def test_search_truncates_content(store):
    sid = store.create_session(title="long")["id"]
    store.add_message(sid, "user", "elephant " * 200)
    hits = store.search_messages("elephant")
    assert hits
    assert len(hits[0]["content"]) <= 320


def test_search_soft_fails_on_bad_db(store, monkeypatch):
    # Point at an un-writable/garbage path — must return [] not raise.
    monkeypatch.setenv("AIFORGE_CHAT_DB_PATH", "/nonexistent-root/x/y/chat.db")
    assert store.search_messages("anything") == []


# ── _chat_session_recall ───────────────────────────────────────────────────

def test_recall_formats_block(store):
    s_kafka, _s_redis, s_cur = _seed(store)
    from aiforge_core.runtime import chat_agent
    block = chat_agent._chat_session_recall("kafka retries", s_cur)
    assert block
    assert "PRIOR CHAT SESSIONS" in block
    assert "Kafka retries" in block          # session title
    assert "kafka" in block.lower()          # content


def test_recall_empty_when_no_hits(store):
    _seed(store)
    from aiforge_core.runtime import chat_agent
    assert chat_agent._chat_session_recall("zzznope", 999) == ""


def test_recall_excludes_current_session(store):
    _s_kafka, _s_redis, s_cur = _seed(store)
    store.add_message(s_cur, "user", "kafka in current session only text")
    from aiforge_core.runtime import chat_agent
    block = chat_agent._chat_session_recall("kafka", s_cur)
    # The current session's own message must not appear.
    assert "current session only" not in block


# ── search_chat_sessions tool ──────────────────────────────────────────────

def test_search_chat_sessions_tool(store):
    _seed(store)
    from aiforge_core.runtime import chat_agent
    assert "search_chat_sessions" in chat_agent.TOOLS
    out = chat_agent.TOOLS["search_chat_sessions"]({"query": "kafka", "limit": 5}, ".")
    assert out["ok"] is True
    assert isinstance(out["hits"], list)
    assert any("kafka" in h["content"].lower() for h in out["hits"])


def test_search_chat_sessions_tool_accepts_q_alias(store):
    _seed(store)
    from aiforge_core.runtime import chat_agent
    out = chat_agent.TOOLS["search_chat_sessions"]({"q": "redis"}, ".")
    assert out["ok"] is True
    assert any("redis" in (h["session_title"] + h["content"]).lower()
               for h in out["hits"])
