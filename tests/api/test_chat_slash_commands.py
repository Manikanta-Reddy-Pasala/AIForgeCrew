"""Integration: a ``/name`` chat message is expanded BEFORE the agent sees it.

Proves the interception in ``chat_session_message`` replaces a slash-command
message with its ``.aiforge/commands/<name>.md`` template (arguments
substituted) so the single-agent path (and, by construction, team/plan which
read the same ``body.content`` / persisted history) run on the expanded prompt.
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
    for k in ("AIFORGE_MEMORY_BACKEND", "AIFORGE_NEO4J_URI", "NEO4J_URI",
              "AIFORGE_WORKSPACE_DIR", "AIFORGE_REPO_ROOT", "AIFORGE_COMMANDS_DIR"):
        monkeypatch.delenv(k, raising=False)
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
            except Exception:
                pass
    return out


def test_slash_command_expands_before_agent(app_client, monkeypatch, tmp_path):
    client, api = app_client

    # A repo with a user command file.
    repo = tmp_path / "repo"
    (repo / ".aiforge" / "commands").mkdir(parents=True)
    (repo / ".aiforge" / "commands" / "deploy.md").write_text(
        "Deploy the app to $ARGUMENTS. Target env: $1.", encoding="utf-8")

    # Keep the run hermetic: no enhancer LLM, no rule-capture LLM, and capture
    # exactly what messages run_chat_agent receives.
    from aiforge_core.runtime import chat_agent
    from aiforge_core.runtime import parallel_subtasks as pp
    from aiforge_core.runtime import rule_capture as rc
    from aiforge_core.runtime import chat_persist
    monkeypatch.setattr(pp, "_enhance", lambda *a, **k: "")       # no-op enhancer
    monkeypatch.setattr(rc, "should_classify", lambda *a, **k: False)
    monkeypatch.setattr(chat_persist, "persist_turn", lambda **kw: None)

    captured: dict = {}

    def _fake_run(messages, **kw):
        captured["messages"] = messages
        return iter([{"type": "message", "text": "ok"}, {"type": "done"}])

    monkeypatch.setattr(chat_agent, "run_chat_agent", _fake_run)

    sid = client.post("/api/chat/sessions",
                      json={"title": "t", "cwd": str(repo)}).json()["id"]
    r = client.post(f"/api/chat/sessions/{sid}/message",
                    json={"content": "/deploy staging now", "mode": "simple"})
    evs = _events(r.text)

    # The agent saw the EXPANDED template, not the raw "/deploy ...".
    msgs = captured.get("messages")
    assert msgs is not None, "run_chat_agent was never called"
    last_user = next(m for m in reversed(msgs) if m.get("role") == "user")
    assert "Deploy the app to staging now." in last_user["content"]
    assert "Target env: staging." in last_user["content"]
    assert "/deploy" not in last_user["content"]

    # A notice event announced the expansion.
    assert any(e.get("role") == "command" for e in evs if e.get("type") == "thought")


def test_builtin_help_answers_inline_without_agent(app_client, monkeypatch, tmp_path):
    client, api = app_client
    repo = tmp_path / "repo"
    (repo / ".aiforge" / "commands").mkdir(parents=True)
    (repo / ".aiforge" / "commands" / "deploy.md").write_text(
        "Deploy $ARGUMENTS", encoding="utf-8")

    from aiforge_core.runtime import chat_agent
    from aiforge_core.runtime import chat_persist
    monkeypatch.setattr(chat_persist, "persist_turn", lambda **kw: None)

    called = {"agent": False}

    def _fake_run(messages, **kw):
        called["agent"] = True
        return iter([{"type": "done"}])

    monkeypatch.setattr(chat_agent, "run_chat_agent", _fake_run)

    sid = client.post("/api/chat/sessions",
                      json={"title": "t", "cwd": str(repo)}).json()["id"]
    r = client.post(f"/api/chat/sessions/{sid}/message",
                    json={"content": "/help", "mode": "simple"})
    evs = _events(r.text)

    assert called["agent"] is False, "/help must not invoke the model"
    msg = next(e for e in evs if e.get("type") == "message")
    assert "/deploy" in msg["text"]


def test_non_command_message_reaches_agent_verbatim(app_client, monkeypatch, tmp_path):
    client, api = app_client
    repo = tmp_path / "repo"

    from aiforge_core.runtime import chat_agent
    from aiforge_core.runtime import parallel_subtasks as pp
    from aiforge_core.runtime import rule_capture as rc
    from aiforge_core.runtime import chat_persist
    monkeypatch.setattr(pp, "_enhance", lambda *a, **k: "")
    monkeypatch.setattr(rc, "should_classify", lambda *a, **k: False)
    monkeypatch.setattr(chat_persist, "persist_turn", lambda **kw: None)

    captured: dict = {}

    def _fake_run(messages, **kw):
        captured["messages"] = messages
        return iter([{"type": "done"}])

    monkeypatch.setattr(chat_agent, "run_chat_agent", _fake_run)

    sid = client.post("/api/chat/sessions",
                      json={"title": "t", "cwd": str(repo)}).json()["id"]
    client.post(f"/api/chat/sessions/{sid}/message",
                json={"content": "/unknown please help", "mode": "simple"}).text

    last_user = next(m for m in reversed(captured["messages"])
                     if m.get("role") == "user")
    assert "/unknown please help" in last_user["content"]
