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
    assert evs[0]["type"] == "attached"
    assert evs[0]["running"] is True
    assert "started_at" in evs[0]   # true run start → continuous reattach timer
    types = [e["type"] for e in evs]
    assert "thought" in types
    assert "tool" in types
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
    assert assistant
    assert assistant[-1]["content"] == "final answer"
    # And once done, the run is no longer reported as running.
    from aiforge_core.runtime import chat_runs
    assert chat_runs.is_running(sid) is False


def test_kill_all_cancels_runs_and_releases_team_lock(app_client):
    """The kill-all escape hatch cancels every tracked run, finishes the live-run
    buffers, and force-releases the team run lock (the 'waiting for another team
    run' wedge)."""
    client, _ = app_client
    from aiforge_core.runtime import chat_cancel, chat_pipeline, chat_runs

    # Simulate two wedged runs + a held team lock.
    chat_cancel.start(901)
    chat_cancel.start(902)
    chat_runs.start(901)
    chat_runs.start(902)
    chat_pipeline._RUN_LOCK.acquire()
    try:
        r = client.post("/api/chat/kill-all").json()
        assert set(r["killed"]) >= {901, 902}
        assert r["team_lock_released"] is True
        # Runs cleared, lock free.
        assert chat_runs.is_running(901) is False
        assert chat_runs.is_running(902) is False
        assert chat_pipeline._RUN_LOCK.locked() is False
        # kill-all now leaves the cancel TOKEN set (so a live producer observes
        # it before tearing down) instead of popping it immediately — the run's
        # own finally pops it in production. These manual tokens have no
        # producer, so clean them up here to not leak into the next test.
        assert chat_cancel.is_cancelled(901) is True
    finally:
        chat_cancel.finish(901)
        chat_cancel.finish(902)
        if chat_pipeline._RUN_LOCK.locked():
            chat_pipeline._RUN_LOCK.release()


def test_fresh_session_gets_model_title(app_client, monkeypatch):
    """A first message on a fresh session upgrades the title to the model's
    suggestion (not just the truncated message)."""
    client, _ = app_client
    from aiforge_core.runtime import chat_agent, chat_title, chat_store
    monkeypatch.setattr(chat_agent, "run_chat_agent",
                        lambda *a, **k: iter([{"type": "message", "text": "ok"},
                                              {"type": "done"}]))
    monkeypatch.setattr(chat_title, "suggest_title",
                        lambda prompt, role="chat": "Deploy API To NUC")
    sid = client.post("/api/chat/sessions", json={"title": "New chat"}).json()["id"]
    client.post(f"/api/chat/sessions/{sid}/message",
                json={"content": "how do I deploy the api", "mode": "simple"})
    # Title is upgraded by a concurrent daemon thread → poll briefly.
    import time as _t
    for _ in range(50):
        if chat_store.get_session(sid)["title"] == "Deploy API To NUC":
            break
        _t.sleep(0.02)
    assert chat_store.get_session(sid)["title"] == "Deploy API To NUC"


def test_kill_all_idempotent_when_nothing_running(app_client):
    client, _ = app_client
    r = client.post("/api/chat/kill-all").json()
    assert r["count"] == 0
    assert r["team_lock_released"] is False


def test_overlapping_message_rejected_409(app_client):
    """A 2nd message while a run is already in flight is rejected (409) — the
    server-side backstop against the cancel-token hijack + double-persist."""
    client, _ = app_client
    from aiforge_core.runtime import chat_runs
    sid = client.post("/api/chat/sessions", json={"title": "t"}).json()["id"]
    chat_runs.start(sid)   # simulate an in-flight run
    try:
        r = client.post(f"/api/chat/sessions/{sid}/message",
                        json={"content": "second", "mode": "simple"})
        assert r.status_code == 409
    finally:
        chat_runs.finish(sid)
