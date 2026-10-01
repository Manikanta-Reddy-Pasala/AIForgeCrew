"""The memory brief goes out with the FIRST message of a chat, not every turn.

Repeating the brief on each turn spent its whole budget again every time. A
follow-up turn is told the brief is not repeated and that memory_lookup is the
way to reach a fact it needs.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def agent(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "cfg" / "memory"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "cfg" / "m.db"))
    from aiforge_core.memory import unified_query as uq
    from aiforge_core.runtime import chat_agent, context_bundle
    monkeypatch.setattr(uq, "query", lambda *a, **k: {"hits": []})
    monkeypatch.setattr(context_bundle, "_project_brief",
                        lambda cwd: "PROJECT MEMORY (shop):\n- ZEBRA-FACT lives here")
    return chat_agent, tmp_path


def _system_prompt(chat_agent, cwd, messages):
    seen = {}

    def fake(role, convo):
        seen["sys"] = convo[0]["content"]
        return "FINAL: ok"

    list(chat_agent.run_chat_agent(messages, cwd=str(cwd), complete_fn=fake))
    return seen["sys"]


_FIRST = [{"role": "user", "content": "explain how the checkout retry works"}]
_FOLLOW_UP = _FIRST + [
    {"role": "assistant", "content": "it retries three times"},
    {"role": "user", "content": "and where is the backoff configured in code"}]


def test_first_message_carries_the_brief(agent):
    chat_agent, cwd = agent
    sys_prompt = _system_prompt(chat_agent, cwd, _FIRST)
    assert "ZEBRA-FACT" in sys_prompt


def test_follow_up_does_not_repeat_it_and_points_at_the_tool(agent):
    chat_agent, cwd = agent
    sys_prompt = _system_prompt(chat_agent, cwd, _FOLLOW_UP)
    assert "ZEBRA-FACT" not in sys_prompt
    assert "is not repeated" in sys_prompt and "memory_lookup" in sys_prompt


def test_every_turn_can_be_restored_by_setting(agent, monkeypatch):
    chat_agent, cwd = agent
    monkeypatch.setenv("AIFORGE_CHAT_BRIEF", "every")
    assert "ZEBRA-FACT" in _system_prompt(chat_agent, cwd, _FOLLOW_UP)
