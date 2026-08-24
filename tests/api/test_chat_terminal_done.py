"""item B — parallel-team and best-of-N chat paths must emit exactly one
terminal {"type":"done"} so a UI waiting on `done` never hangs.

The underlying generators (stream_parallel_team / stream_best_of_n) emit NO
terminal done; the endpoint synthesizes one after each `yield from`. This test
mocks those generators to yield only a `message` (no done) and asserts the SSE
stream carries exactly one done.
"""
import importlib
import json

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
    monkeypatch.setenv("AIFORGE_PARALLEL_SUBTASKS", "1")
    monkeypatch.setenv("AIFORGE_BEST_OF_N", "2")
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


def _done_count(text: str) -> int:
    n = 0
    for line in text.splitlines():
        if line.startswith("data: "):
            try:
                ev = json.loads(line[6:])
            except ValueError:
                continue
            if ev.get("type") == "done":
                n += 1
    return n


def test_best_of_n_path_emits_single_done(app_client, monkeypatch):
    client, api = app_client
    from aiforge_core.runtime import best_of_n as bon
    from aiforge_core.runtime import parallel_subtasks as pp
    monkeypatch.setattr(pp, "_enhance", lambda *a, **k: "spec")
    monkeypatch.setattr(pp, "_architect", lambda *a, **k: [])
    monkeypatch.setattr(pp, "_plan_files", lambda *a, **k: [])
    monkeypatch.setattr(pp, "_decompose", lambda *a, **k: [])     # <2 → best-of-N
    # generator yields NO done — the endpoint must synthesize one.
    monkeypatch.setattr(bon, "stream_best_of_n",
                        lambda *a, **k: iter([{"type": "message", "text": "x"}]))

    sid = client.post("/api/chat/sessions", json={"title": "t"}).json()["id"]
    r = client.post(f"/api/chat/sessions/{sid}/message",
                    json={"content": "build one hard thing", "mode": "team"})
    assert _done_count(r.text) == 1


def test_parallel_team_path_emits_single_done(app_client, monkeypatch):
    client, api = app_client
    from aiforge_core.runtime import parallel_subtasks as pp
    monkeypatch.setattr(pp, "_enhance", lambda *a, **k: "spec")
    monkeypatch.setattr(pp, "_architect", lambda *a, **k:
                        [{"path": "a.py", "purpose": "a"},
                         {"path": "b.py", "purpose": "b"}])
    monkeypatch.setattr(pp, "_plan_files", lambda *a, **k:
                        [{"slug": "a", "goal": "a"}, {"slug": "b", "goal": "b"}])
    # generator yields NO done — the endpoint must synthesize one.
    monkeypatch.setattr(pp, "stream_parallel_team",
                        lambda *a, **k: iter([{"type": "message", "text": "x"}]))

    sid = client.post("/api/chat/sessions", json={"title": "t"}).json()["id"]
    r = client.post(f"/api/chat/sessions/{sid}/message",
                    json={"content": "build two files", "mode": "team"})
    assert _done_count(r.text) == 1


def test_cancelled_best_of_n_one_done_and_no_stuck_rows(app_client, monkeypatch):
    """item 3 — a Stop mid best-of-N still yields exactly one terminal done AND
    persists no pending/running subtask rows (they're reconciled to failed)."""
    client, api = app_client
    from aiforge_core.runtime import best_of_n as bon
    from aiforge_core.runtime import chat_cancel
    from aiforge_core.runtime import parallel_subtasks as pp
    monkeypatch.setattr(pp, "_enhance", lambda *a, **k: "spec")
    monkeypatch.setattr(pp, "_architect", lambda *a, **k: [])
    monkeypatch.setattr(pp, "_plan_files", lambda *a, **k: [])
    monkeypatch.setattr(pp, "_decompose", lambda *a, **k: [])     # <2 → best-of-N

    def fake_best(*a, **k):
        sid = k.get("session_id")
        yield {"type": "subtasks", "items": [
            {"slug": "s1", "goal": "g1", "status": "running"},
            {"slug": "s2", "goal": "g2", "status": "pending"}]}
        chat_cancel.cancel(sid)          # user presses Stop mid-run
        yield {"type": "message", "text": "partial"}
        yield {"type": "done"}           # never reached — loop breaks first
    monkeypatch.setattr(bon, "stream_best_of_n", fake_best)

    sid = client.post("/api/chat/sessions", json={"title": "t"}).json()["id"]
    r = client.post(f"/api/chat/sessions/{sid}/message",
                    json={"content": "build one hard thing", "mode": "team"})
    assert _done_count(r.text) == 1      # exactly one terminal done, UI unblocks

    # Persisted/reloaded panel has NO stuck pending/running rows.
    from aiforge_core.runtime import chat_store
    msgs = chat_store.get_messages(sid)
    panels = [s for m in msgs if m["role"] == "assistant"
              for s in m["steps"] if s.get("type") == "subtasks"]
    assert panels, "subtask panel was persisted"
    statuses = {it["status"] for p in panels for it in p["items"]}
    assert statuses, statuses
    assert statuses <= {"done", "failed", "skipped", "won"}, statuses
