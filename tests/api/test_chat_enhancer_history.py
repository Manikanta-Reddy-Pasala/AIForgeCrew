"""item 2 — the simple/plan enhancer must SEE the conversation history (so a
context-dependent follow-up like "no, use postgres instead" resolves against
the prior turns). The earlier "drop folded turns" trim was a REGRESSION: it
produced two consecutive user turns (breaking claude_local alternation) and
lost context when `_enhance` no-ops on a trivial follow-up. The fix replaces
the LAST user turn's content in-place and keeps every prior turn.

Asserts: (1) `_enhance` is called with `history=` carrying the prior turns;
(2) the history passed to `run_chat_agent` keeps all prior turns, replaces only
the last user turn with the enriched spec, and never has two consecutive user
turns.
"""
import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_client(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_PG_URL", raising=False)
    monkeypatch.delenv("AIFORGE_FORCE_PG", raising=False)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    for k in ("AIFORGE_MEMORY_BACKEND", "AIFORGE_NEO4J_URI", "NEO4J_URI"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("AIFORGE_PARALLEL_SUBTASKS", raising=False)
    monkeypatch.delenv("AIFORGE_BEST_OF_N", raising=False)
    import aiforge_core.config.env as envmod
    importlib.reload(envmod)
    import aiforge_core.tickets.backend_factory as bf
    importlib.reload(bf)
    bf.reset_backend_for_tests()
    import aiforge_core.tickets.store as store
    importlib.reload(store)
    import aiforge_core.api.api as api
    importlib.reload(api)
    return TestClient(api.app), api


def test_followup_enhancer_sees_history_no_double_fold(app_client, monkeypatch):
    client, api = app_client
    from aiforge_core.runtime import chat_agent
    from aiforge_core.runtime import parallel_subtasks as pp

    enhance_calls: list = []
    agent_histories: list = []

    def fake_enhance(prompt, *, history=None, cwd=None, repo=None):
        enhance_calls.append({"prompt": prompt, "history": history})
        return f"SPEC<{prompt}>"

    def fake_run_chat_agent(history, **kw):
        agent_histories.append([dict(m) for m in history])
        yield {"type": "message", "text": "ok"}
        yield {"type": "done"}

    monkeypatch.setattr(pp, "_enhance", fake_enhance)
    monkeypatch.setattr(chat_agent, "run_chat_agent", fake_run_chat_agent)

    sid = client.post("/api/chat/sessions", json={"title": "t"}).json()["id"]
    # turn 1 — establishes context.
    client.post(f"/api/chat/sessions/{sid}/message",
                json={"content": "build a todo app", "mode": "act"}).text
    # turn 2 — a context-dependent follow-up.
    client.post(f"/api/chat/sessions/{sid}/message",
                json={"content": "no, use postgres instead", "mode": "act"}).text

    # (1) The follow-up's enhancer call SAW the prior turns (referent resolution).
    last = enhance_calls[-1]
    assert last["prompt"] == "no, use postgres instead"
    hist = last["history"] or []
    joined = " ".join(m.get("content", "") for m in hist)
    assert "todo app" in joined          # context available to the enhancer

    # (2) Replace-in-place: the history handed to the agent KEEPS every prior
    # turn, with only the LAST user turn's content swapped for the enriched
    # spec. No turn is dropped, and alternation stays intact (no two consecutive
    # user turns).
    agent_hist = agent_histories[-1]
    assert agent_hist[-1]["role"] == "user"
    assert agent_hist[-1]["content"] == "SPEC<no, use postgres instead>"
    # the original turn-1 user message is still present (context not lost)
    assert any(m["content"] == "build a todo app" for m in agent_hist)
    # no two consecutive same-role turns (would break claude_local alternation)
    roles = [m["role"] for m in agent_hist]
    assert all(roles[i] != roles[i + 1] for i in range(len(roles) - 1))
