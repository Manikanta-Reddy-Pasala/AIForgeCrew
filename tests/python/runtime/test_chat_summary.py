"""Per-session chat summaries → browsable md file + memory graph.

``chat_summary.summarize_session`` distils ONE concise markdown summary of a
chat session (cheap-tier LLM, capped transcript + output) and persists it to
BOTH an md file (via ``md_store.upsert_section``) AND the configured memory
backend/graph (via ``memory_write``). Boundary-gated + soft-fail everywhere —
a summary must never break or slow a chat turn.
"""
import importlib

import pytest


@pytest.fixture
def store(monkeypatch, tmp_path):
    """A fresh, isolated chat DB per test."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_CHAT_DB_PATH", str(tmp_path / "chat.db"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "memory"))
    monkeypatch.delenv("AIFORGE_CHAT_SUMMARY", raising=False)
    import aiforge_core.runtime.chat_store as cs
    importlib.reload(cs)
    return cs


@pytest.fixture
def captures(monkeypatch):
    """Capture md_store.upsert_section + memory_write calls; stub the LLM."""
    md_calls: list[dict] = []
    mw_calls: list[dict] = []
    llm_holder: dict = {"reply": "## Topic\n\n- discussed the kafka retry design",
                        "raise": False}

    def fake_upsert(**kw):
        md_calls.append(kw)
        return {"ok": True, "file": "chat-session-1.md"}

    def fake_memory_write(text, **kw):
        mw_calls.append({"text": text, **kw})
        return {"ok": True, "id": "x", "label": "Observation_v2"}

    def fake_complete(role, messages, **kw):
        if llm_holder["raise"]:
            raise RuntimeError("model down")
        return llm_holder["reply"]

    monkeypatch.setattr("aiforge_core.memory.md_store.upsert_section", fake_upsert)
    monkeypatch.setattr(
        "aiforge_core.runtime.tools.memory_write.memory_write", fake_memory_write)
    monkeypatch.setattr("aiforge_core.llm.client.complete", fake_complete)
    return {"md": md_calls, "mw": mw_calls, "llm": llm_holder}


def _seed(cs, n_turns=4):
    sid = cs.create_session(title="Kafka retries")["id"]
    for i in range(n_turns):
        role = "user" if i % 2 == 0 else "assistant"
        cs.add_message(sid, role, f"message number {i} about kafka retries and backoff")
    return sid


def _summary_mod():
    import aiforge_core.runtime.chat_summary as csum
    importlib.reload(csum)
    return csum


# ── happy path: writes BOTH md + graph ─────────────────────────────────────

def test_writes_both_md_and_graph(store, captures):
    sid = _seed(store, 4)
    out = _summary_mod().summarize_session(sid, "myrepo")
    assert out["ok"] is True
    # md_store.upsert_section called with the session source + summary body.
    assert len(captures["md"]) == 1
    md = captures["md"][0]
    assert md["source"] == f"chat-session:{sid}"
    assert md["kind"] == "chat_summary"
    assert "kafka" in md["section_body"].lower()
    assert md["repo"] == "myrepo"
    assert f"session:{sid}" in md["tags"]
    # memory_write (graph/backend) called with the same summary text.
    assert len(captures["mw"]) == 1
    mw = captures["mw"][0]
    assert str(sid) in mw["text"]
    assert "kafka" in mw["text"].lower()
    assert mw["kind"] == "chat_summary"
    assert f"session:{sid}" in mw["tags"]
    assert mw["repo"] == "myrepo"


# ── too-short session → skipped, no LLM/persist ────────────────────────────

def test_too_short_session_skipped(store, captures):
    sid = _seed(store, 2)     # < min_turns default 4
    out = _summary_mod().summarize_session(sid, "myrepo")
    assert out["ok"] is True
    assert out.get("skipped") == "too_short"
    assert captures["md"] == []
    assert captures["mw"] == []


# ── empty LLM summary → written 0, no persist ──────────────────────────────

def test_empty_summary_writes_nothing(store, captures):
    sid = _seed(store, 4)
    captures["llm"]["reply"] = "   \n  "
    out = _summary_mod().summarize_session(sid, "myrepo")
    assert out["ok"] is True
    assert out.get("written") == 0
    assert captures["md"] == []
    assert captures["mw"] == []


# ── kill switch ────────────────────────────────────────────────────────────

def test_disabled_via_env(store, captures, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_SUMMARY", "0")
    sid = _seed(store, 4)
    out = _summary_mod().summarize_session(sid, "myrepo")
    assert out["ok"] is False
    assert out.get("skipped") == "disabled"
    assert captures["md"] == []
    assert captures["mw"] == []


# ── soft-fail: LLM raises ──────────────────────────────────────────────────

def test_soft_fails_when_llm_raises(store, captures):
    sid = _seed(store, 4)
    captures["llm"]["raise"] = True
    out = _summary_mod().summarize_session(sid, "myrepo")   # must not raise
    assert out["ok"] is False
    assert "error" in out
    assert captures["md"] == []


# ── soft-fail: md_store raises (still no raise; graph attempt independent) ──

def test_soft_fails_when_md_store_raises(store, captures, monkeypatch):
    sid = _seed(store, 4)

    def boom(**kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr("aiforge_core.memory.md_store.upsert_section", boom)
    out = _summary_mod().summarize_session(sid, "myrepo")   # must not raise
    assert out["ok"] is False
    assert "error" in out
