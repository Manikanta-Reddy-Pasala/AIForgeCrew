"""spawn_task starts a side agent for an independent part; it never nests."""
import pytest

from aiforge_core.runtime import request_context
from aiforge_core.runtime.chat_agent._tools import _spawn


class _Store:
    sessions = {}

    @classmethod
    def get_session(cls, sid):
        return cls.sessions.get(sid)


@pytest.fixture
def wired(monkeypatch):
    from aiforge_core.runtime import chat_store
    from aiforge_core.api.routes._chat import _side_tasks
    created = []
    monkeypatch.setattr(chat_store, "get_session", _Store.get_session)
    monkeypatch.setattr(_side_tasks, "create",
                        lambda sid, text, mode: created.append((sid, text, mode))
                        or {"id": 99, "task": {"state": "running"}})
    monkeypatch.setattr(request_context, "get_session_id", lambda: "7")
    _Store.sessions = {7: {"id": 7}}
    return created


def test_it_starts_a_side_task_under_the_current_chat(wired):
    out = _spawn._t_spawn_task({"task": "Summarise how the retry layer works"}, ".")
    assert out["ok"] and out["task_id"] == 99 and out["state"] == "running"
    assert wired == [(7, "Summarise how the retry layer works", "simple")]
    assert "continue with your own part" in out["note"]


def test_a_side_task_does_not_start_more(wired):
    _Store.sessions = {7: {"id": 7, "parent_id": 3}}
    out = _spawn._t_spawn_task({"task": "Summarise how the retry layer works"}, ".")
    assert not out["ok"] and wired == []


def test_no_session_or_a_tiny_task_is_refused(monkeypatch, wired):
    assert not _spawn._t_spawn_task({"task": "x"}, ".")["ok"]
    monkeypatch.setattr(request_context, "get_session_id", lambda: None)
    assert not _spawn._t_spawn_task({"task": "Summarise how the retry layer works"}, ".")["ok"]


def test_it_is_a_registered_core_act_tool():
    from aiforge_core.runtime.chat_agent._registry import TOOLS
    from aiforge_core.runtime.chat_agent._tools._schemas import _CORE_ACT, _CORE_READ
    assert "spawn_task" in TOOLS and "spawn_task" in _CORE_ACT
    assert "spawn_task" not in _CORE_READ                # plan/analyze stay read-only
