"""Simple/plan mode enhancer skip-on-followup gate.

The enhancer (`_pp._enhance`) is mandatory on a session's first turn, but a
small follow-up ("fix that", "rename it") shouldn't pay for a second LLM
round-trip (+ the memory recall inside `_enhance`) every single message.
`turn_router.classify` (already used to auto-downgrade team turns) is reused
to decide: follow-up + classifies "simple" => skip `_enhance`, use the raw
prompt. First turn, or a follow-up that classifies "complex", or a classify
failure => `_enhance` still runs (safe default).
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
    monkeypatch.setenv("AIFORGE_PARALLEL_SUBTASKS", "0")  # single-agent path under test (default is now ON)
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


def _wire(monkeypatch, classify_return):
    from aiforge_core.runtime import chat_agent
    from aiforge_core.runtime import parallel_subtasks as pp
    from aiforge_core.runtime import turn_router

    enhance_calls = []

    def fake_enhance(prompt, *, history=None, cwd=None, repo=None):
        enhance_calls.append(prompt)
        return f"SPEC<{prompt}>"

    def fake_run_chat_agent(history, **kw):
        yield {"type": "message", "text": "ok"}
        yield {"type": "done"}

    monkeypatch.setattr(pp, "_enhance", fake_enhance)
    monkeypatch.setattr(chat_agent, "run_chat_agent", fake_run_chat_agent)
    monkeypatch.setattr(turn_router, "classify",
                        lambda *a, **k: classify_return)
    return enhance_calls


def test_first_turn_always_enhances(app_client, monkeypatch):
    client, _ = app_client
    enhance_calls = _wire(monkeypatch, "simple")
    sid = client.post("/api/chat/sessions", json={"title": "t"}).json()["id"]
    client.post(f"/api/chat/sessions/{sid}/message",
                json={"content": "build a todo app", "mode": "act"})
    assert enhance_calls == ["build a todo app"]


def test_simple_followup_skips_enhancer(app_client, monkeypatch):
    client, _ = app_client
    enhance_calls = _wire(monkeypatch, "simple")
    sid = client.post("/api/chat/sessions", json={"title": "t"}).json()["id"]
    client.post(f"/api/chat/sessions/{sid}/message",
                json={"content": "build a todo app", "mode": "act"})
    enhance_calls.clear()
    client.post(f"/api/chat/sessions/{sid}/message",
                json={"content": "rename the var to foo", "mode": "act"})
    assert enhance_calls == []          # enhancer skipped on this follow-up


def test_complex_followup_still_enhances(app_client, monkeypatch):
    client, _ = app_client
    enhance_calls = _wire(monkeypatch, "complex")
    sid = client.post("/api/chat/sessions", json={"title": "t"}).json()["id"]
    client.post(f"/api/chat/sessions/{sid}/message",
                json={"content": "build a todo app", "mode": "act"})
    enhance_calls.clear()
    client.post(f"/api/chat/sessions/{sid}/message",
                json={"content": "no, use postgres instead", "mode": "act"})
    assert enhance_calls == ["no, use postgres instead"]


def test_classify_failure_keeps_enhancer_mandatory(app_client, monkeypatch):
    client, _ = app_client
    from aiforge_core.runtime import chat_agent
    from aiforge_core.runtime import parallel_subtasks as pp
    from aiforge_core.runtime import turn_router

    enhance_calls = []

    def fake_enhance(prompt, *, history=None, cwd=None, repo=None):
        enhance_calls.append(prompt)
        return f"SPEC<{prompt}>"

    def fake_run_chat_agent(history, **kw):
        yield {"type": "message", "text": "ok"}
        yield {"type": "done"}

    def boom(*a, **k):
        raise RuntimeError("classifier down")

    monkeypatch.setattr(pp, "_enhance", fake_enhance)
    monkeypatch.setattr(chat_agent, "run_chat_agent", fake_run_chat_agent)
    monkeypatch.setattr(turn_router, "classify", boom)

    sid = client.post("/api/chat/sessions", json={"title": "t"}).json()["id"]
    client.post(f"/api/chat/sessions/{sid}/message",
                json={"content": "build a todo app", "mode": "act"})
    enhance_calls.clear()
    client.post(f"/api/chat/sessions/{sid}/message",
                json={"content": "tweak it", "mode": "act"})
    assert enhance_calls == ["tweak it"]     # classify raised => stayed mandatory
