"""The /attach endpoint — resume an in-flight chat run after navigating away.

* No run in flight  → first event {attached, running:false} then done.
* Run in flight      → {attached, running:true}, replays buffered events, then
  tails live ones to completion (so a returning client loses no progress).
* A simple-mode message run persists its turn from the background producer
  thread even though the client only tails a subscriber (the navigate-away
  survival guarantee).
"""
import importlib
import json
import threading
import time

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
    # Skip the LLM enhancer + auto-memory so the simple path is deterministic.
    monkeypatch.setenv("AIFORGE_ENHANCER_DISABLE", "1")
    monkeypatch.setenv("AIFORGE_CHAT_AUTO_MEMORY", "0")
    monkeypatch.setenv("AIFORGE_CHAT_AUTO_CHECKPOINT", "0")
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


def _events(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        if line.startswith("data: "):
            try:
                out.append(json.loads(line[6:]))
            except ValueError:
                pass
    return out


def test_attach_no_run_reports_not_running(app_client):
    client, _ = app_client
    sid = client.post("/api/chat/sessions", json={"title": "t"}).json()["id"]
    r = client.get(f"/api/chat/sessions/{sid}/attach")
    evs = _events(r.text)
    assert evs[0] == {"type": "attached", "running": False}
    assert any(e["type"] == "done" for e in evs)


def test_attach_live_run_replays_buffer_then_tails(app_client):
    client, _ = app_client
    from aiforge_core.runtime import chat_runs
    sid = client.post("/api/chat/sessions", json={"title": "t"}).json()["id"]

    # Simulate a run already in flight with buffered progress.
    run = chat_runs.start(sid)
    run.publish({"type": "thought", "text": "working"})
    run.publish({"type": "tool", "name": "editor", "args": {}, "result": {}})

    # Finish it shortly after the client attaches, so the GET replays the
    # buffer then tails the final events to completion.
    def finisher():
        time.sleep(0.15)
        run.publish({"type": "message", "text": "all done"})
        run.publish({"type": "done"})
        run.finish()
    threading.Thread(target=finisher, daemon=True).start()

    r = client.get(f"/api/chat/sessions/{sid}/attach")
    evs = _events(r.text)
    assert evs[0] == {"type": "attached", "running": True}
    types = [e["type"] for e in evs]
    assert "thought" in types and "tool" in types          # buffer replayed
    assert any(e["type"] == "message" and e["text"] == "all done"
               for e in evs)                                # live tail caught
    assert types[-1] == "done"


def test_simple_run_persists_from_background_thread(app_client, monkeypatch):
    """A simple-mode turn runs on a background producer thread and persists its
    result — the foundation of surviving a navigate-away. We consume the stream
    normally here; the assistant turn must be persisted to the store."""
    client, api = app_client
    from aiforge_core.runtime import chat_agent
    monkeypatch.setattr(
        chat_agent, "run_chat_agent",
        lambda *a, **k: iter([
            {"type": "thought", "text": "thinking"},
            {"type": "message", "text": "final answer"},
            {"type": "done"},
        ]))

    sid = client.post("/api/chat/sessions", json={"title": "t"}).json()["id"]
    r = client.post(f"/api/chat/sessions/{sid}/message",
                    json={"content": "do a thing", "mode": "simple"})
    evs = _events(r.text)
    assert any(e["type"] == "message" and e["text"] == "final answer"
               for e in evs)
    assert sum(1 for e in evs if e["type"] == "done") == 1

    from aiforge_core.runtime import chat_store
    msgs = chat_store.get_messages(sid)
    assistant = [m for m in msgs if m["role"] == "assistant"]
    assert assistant and assistant[-1]["content"] == "final answer"
    # And once done, the run is no longer reported as running.
    from aiforge_core.runtime import chat_runs
    assert chat_runs.is_running(sid) is False
