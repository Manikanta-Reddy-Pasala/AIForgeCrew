"""The team-mode auto-downgrade classify (turn_router.should_downgrade_team)
used to run in the SYNCHRONOUS request handler, before the StreamingResponse
even opened — a slow/unreachable classify LLM left the client with zero
bytes, looking hung. It's now deferred to the top of `_produce()` (the
background thread), so the response streams immediately regardless.

This asserts the deferred move didn't change behavior: a team turn that
`should_downgrade_team` flags still runs the simple/plan single-agent path
(not the team pipeline), and the review-edits gate (which depends on the
post-classify `team` value) still ends up correctly ON for the downgraded
turn — both are now set inside `_produce()`, not before it.
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


def test_team_turn_downgraded_runs_simple_path_and_sets_review_edits(
        app_client, monkeypatch):
    client, _ = app_client
    from aiforge_core.runtime import chat_agent, chat_approve, chat_pipeline
    from aiforge_core.runtime import parallel_subtasks as pp
    from aiforge_core.runtime import turn_router

    pipeline_calls = []
    review_edits_seen = []

    def fake_stream_chat_pipeline(*a, **k):
        pipeline_calls.append(1)
        yield {"type": "message", "text": "should not run"}
        yield {"type": "done"}

    def fake_enhance(prompt, *, history=None, cwd=None, repo=None):
        return prompt

    def fake_run_chat_agent(history, session_id=None, **kw):
        review_edits_seen.append(chat_approve.review_edits(session_id))
        yield {"type": "message", "text": "ok"}
        yield {"type": "done"}

    monkeypatch.setattr(chat_pipeline, "stream_chat_pipeline",
                        fake_stream_chat_pipeline)
    monkeypatch.setattr(pp, "_enhance", fake_enhance)
    monkeypatch.setattr(chat_agent, "run_chat_agent", fake_run_chat_agent)
    monkeypatch.setattr(turn_router, "should_downgrade_team",
                        lambda *a, **k: True)

    sid = client.post("/api/chat/sessions", json={"title": "t"}).json()["id"]
    r = client.post(f"/api/chat/sessions/{sid}/message",
                    json={"content": "rename the var", "mode": "team"})
    assert r.status_code == 200

    assert pipeline_calls == []          # never ran the team pipeline
    assert review_edits_seen == [True]   # not-team => review edits gate ON
