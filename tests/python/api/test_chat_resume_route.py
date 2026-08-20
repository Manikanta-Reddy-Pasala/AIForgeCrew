"""A retry after a stopped turn must reach the agent as a RESUME.

The unit tests cover the brief itself; this pins the WIRING — the feature's
whole risk surface. It lives under tests/python because that is what
``testpaths`` in pyproject.toml runs: the first version sat in tests/api and
was never executed by the default suite.
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
    monkeypatch.setenv("AIFORGE_CHAT_DB_PATH", str(tmp_path / "chat.db"))
    monkeypatch.setenv("AIFORGE_CHAT_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    for k in ("AIFORGE_MEMORY_BACKEND", "AIFORGE_NEO4J_URI", "NEO4J_URI"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AIFORGE_PARALLEL_SUBTASKS", "0")
    monkeypatch.delenv("AIFORGE_BEST_OF_N", raising=False)
    # Quiet the per-turn background work. These fire threads that write the
    # same SQLite store the assertions read, and under a loaded full-suite run
    # one of them could still be mid-write when the next request builds its
    # history — which is how this file failed once in CI-scale runs while
    # passing every time in isolation. None of it is under test here.
    monkeypatch.setenv("AIFORGE_CHAT_AUTO_MEMORY", "0")
    monkeypatch.setenv("AIFORGE_CHAT_TITLE", "0")
    monkeypatch.setenv("AIFORGE_CHAT_SUMMARY", "0")
    monkeypatch.setenv("AIFORGE_CHAT_LEARNER", "0")
    monkeypatch.setenv("AIFORGE_LLM_MAX_RPM", "0")
    import aiforge_core.config.env as envmod
    importlib.reload(envmod)
    import aiforge_core.tickets.backend_factory as bf
    importlib.reload(bf)
    bf.reset_backend_for_tests()
    import aiforge_core.tickets.store as store
    importlib.reload(store)
    import aiforge_core.api.api as api
    importlib.reload(api)
    return TestClient(api.app)


def _capture(monkeypatch):
    from aiforge_core.runtime import chat_agent
    from aiforge_core.runtime import parallel_subtasks as pp
    seen: list = []

    def fake_enhance(prompt, *, history=None, cwd=None, repo=None):
        return prompt

    def fake_run_chat_agent(history, session_id=None, **kw):
        seen.append(history)
        # A real edit that LANDED, then the runaway-cap banner: the exact shape
        # of the turn a user then presses Retry on.
        yield {"type": "tool", "name": "file_write",
               "args": {"path": "parser.py"}, "result": {"ok": True}}
        yield {"type": "message", "text": "(stopped: hit the runaway safety cap)"}
        yield {"type": "done"}

    monkeypatch.setattr(pp, "_enhance", fake_enhance)
    monkeypatch.setattr(chat_agent, "run_chat_agent", fake_run_chat_agent)
    return seen


def _last_user(history):
    return next(m["content"] for m in reversed(history) if m["role"] == "user")


def _assert_stopped_turn_persisted(sid):
    """The brief is built from the PERSISTED turn, so a test that asserts on
    the brief is really asserting two things. Check the precondition
    separately: a failure then says which half broke."""
    from aiforge_core.runtime import chat_store
    from aiforge_core.runtime import chat_resume
    rows = chat_store.get_messages(sid)
    assert chat_resume.last_stopped_turn(rows) is not None, \
        f"the stopped turn was not persisted: {rows}"


def test_retry_after_a_stopped_turn_carries_the_resume_brief(app_client, monkeypatch):
    seen = _capture(monkeypatch)
    client = app_client
    sid = client.post("/api/chat/sessions", json={"title": "t"}).json()["id"]

    # Turn 1 stops after an edit that DID land.
    assert client.post(f"/api/chat/sessions/{sid}/message",
                       json={"content": "build the parser"}).status_code == 200

    # Turn 2 = the same words again (what Retry sends).
    assert client.post(f"/api/chat/sessions/{sid}/message",
                       json={"content": "build the parser"}).status_code == 200
    brief = _last_user(seen[-1])
    assert "[RESUME]" in brief
    assert "parser.py" in brief          # the file it must not rewrite blindly


def test_a_normal_follow_up_is_untouched(app_client, monkeypatch):
    seen = _capture(monkeypatch)
    client = app_client
    sid = client.post("/api/chat/sessions", json={"title": "t"}).json()["id"]
    assert client.post(f"/api/chat/sessions/{sid}/message",
                       json={"content": "build the parser"}).status_code == 200
    # DIFFERENT words → a follow-up, not a retry: no brief, no "finish only
    # what is pending" instruction hijacking a fresh request.
    assert client.post(f"/api/chat/sessions/{sid}/message",
                       json={"content": "now add tests"}).status_code == 200
    assert "[RESUME]" not in _last_user(seen[-1])


def test_start_over_forces_a_clean_rerun(app_client, monkeypatch):
    """resume=false is the escape hatch: the partial work may be junk."""
    seen = _capture(monkeypatch)
    client = app_client
    sid = client.post("/api/chat/sessions", json={"title": "t"}).json()["id"]
    assert client.post(f"/api/chat/sessions/{sid}/message",
                       json={"content": "build the parser"}).status_code == 200
    assert client.post(f"/api/chat/sessions/{sid}/message",
                       json={"content": "build the parser",
                             "resume": False}).status_code == 200
    assert "[RESUME]" not in _last_user(seen[-1])


def test_a_user_stop_is_resumable_even_with_no_banner(app_client, monkeypatch):
    """The Stop button leaves no "(stopped:" text — the structural marker
    persisted with the turn is what makes this case work at all."""
    from aiforge_core.runtime import chat_agent, chat_store
    from aiforge_core.runtime import parallel_subtasks as pp
    seen: list = []

    def fake_enhance(prompt, *, history=None, cwd=None, repo=None):
        return prompt

    def fake_run(history, session_id=None, **kw):
        seen.append(history)
        yield {"type": "tool", "name": "file_write",
               "args": {"path": "parser.py"}, "result": {"ok": True}}
        yield {"type": "error", "text": "stopped by user"}
        yield {"type": "done"}

    monkeypatch.setattr(pp, "_enhance", fake_enhance)
    monkeypatch.setattr(chat_agent, "run_chat_agent", fake_run)
    client = app_client
    sid = client.post("/api/chat/sessions", json={"title": "t"}).json()["id"]
    assert client.post(f"/api/chat/sessions/{sid}/message",
                       json={"content": "build the parser"}).status_code == 200
    row = [r for r in chat_store.get_messages(sid) if r["role"] == "assistant"][-1]
    assert not (row.get("content") or "").startswith("(stopped")   # no banner
    assert client.post(f"/api/chat/sessions/{sid}/message",
                       json={"content": "build the parser"}).status_code == 200
    assert "[RESUME]" in _last_user(seen[-1])


def test_resume_flag_forces_it_after_a_rephrase(app_client, monkeypatch):
    seen = _capture(monkeypatch)
    client = app_client
    sid = client.post("/api/chat/sessions", json={"title": "t"}).json()["id"]
    assert client.post(f"/api/chat/sessions/{sid}/message",
                       json={"content": "build the parser"}).status_code == 200
    _assert_stopped_turn_persisted(sid)
    assert client.post(f"/api/chat/sessions/{sid}/message",
                       json={"content": "carry on with it",
                             "resume": True}).status_code == 200
    assert "[RESUME]" in _last_user(seen[-1])
