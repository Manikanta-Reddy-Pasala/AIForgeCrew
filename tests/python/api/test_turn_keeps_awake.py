"""Every chat turn holds the machine awake, and lets go afterwards.

The two slowest paths (team runs, scheduled jobs) hold it themselves. Doing it
for the turn as well is what makes the answer to "will my work survive me
locking the screen" yes for anything a user can start — and the refcount makes
the overlap free.

The leak is the thing worth pinning: an acquire that outlives its turn leaves a
laptop awake indefinitely, which the user notices days later as a battery
mystery and never connects to us.
"""
import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_client(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_PG_URL", raising=False)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("AIFORGE_CHAT_DB_PATH", str(tmp_path / "chat.db"))
    monkeypatch.setenv("AIFORGE_CHAT_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("AIFORGE_CHAT_AUTO_MEMORY", "0")
    monkeypatch.setenv("AIFORGE_CHAT_TITLE", "0")
    monkeypatch.setenv("AIFORGE_CHAT_SUMMARY", "0")
    monkeypatch.setenv("AIFORGE_CHAT_LEARNER", "0")
    monkeypatch.setenv("AIFORGE_PARALLEL_SUBTASKS", "0")
    import aiforge_core.config.env as envmod
    importlib.reload(envmod)
    import aiforge_core.tickets.backend_factory as bf
    importlib.reload(bf)
    bf.reset_backend_for_tests()
    import aiforge_core.api.api as api
    importlib.reload(api)
    return TestClient(api.app)


def test_a_turn_holds_the_machine_awake_and_releases_it(app_client, monkeypatch):
    from aiforge_core.runtime import chat_agent, keep_awake
    from aiforge_core.runtime import parallel_subtasks as pp

    events: list = []
    monkeypatch.setattr(keep_awake, "_command", lambda: None)  # no real child
    monkeypatch.setattr(keep_awake, "acquire",
                        lambda: events.append("acquire"))
    monkeypatch.setattr(keep_awake, "release",
                        lambda: events.append("release"))
    monkeypatch.setattr(pp, "_enhance",
                        lambda prompt, **_k: prompt)

    def _fake_run(history, session_id=None, **kw):
        yield {"type": "message", "text": "done"}
        yield {"type": "done"}

    monkeypatch.setattr(chat_agent, "run_chat_agent", _fake_run)

    client = app_client
    sid = client.post("/api/chat/sessions", json={"title": "t"}).json()["id"]
    assert client.post(f"/api/chat/sessions/{sid}/message",
                       json={"content": "hello"}).status_code == 200

    assert events[0] == "acquire"
    assert "release" in events, "a turn that never releases leaves the box awake"
    assert events.count("acquire") == events.count("release")


def test_a_turn_that_blows_up_still_releases(app_client, monkeypatch):
    """The release lives in the producer's finally for this reason: a turn that
    dies mid-flight must not leave the assertion held for the life of the
    process."""
    from aiforge_core.runtime import chat_agent, keep_awake
    from aiforge_core.runtime import parallel_subtasks as pp

    events: list = []
    monkeypatch.setattr(keep_awake, "_command", lambda: None)
    monkeypatch.setattr(keep_awake, "acquire", lambda: events.append("acquire"))
    monkeypatch.setattr(keep_awake, "release", lambda: events.append("release"))
    monkeypatch.setattr(pp, "_enhance", lambda prompt, **_k: prompt)

    def _explode(history, session_id=None, **kw):
        raise RuntimeError("the turn died")
        yield  # pragma: no cover

    monkeypatch.setattr(chat_agent, "run_chat_agent", _explode)

    client = app_client
    sid = client.post("/api/chat/sessions", json={"title": "t"}).json()["id"]
    client.post(f"/api/chat/sessions/{sid}/message", json={"content": "hi"})
    assert events.count("release") == events.count("acquire") >= 1
