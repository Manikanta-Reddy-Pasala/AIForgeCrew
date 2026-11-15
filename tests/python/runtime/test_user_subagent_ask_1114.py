"""When the user asks for a sub-agent, the turn is told to spawn one."""
from __future__ import annotations

import pytest

from aiforge_core.runtime.chat_agent import _prompt_text as P
from aiforge_core.runtime.chat_agent._turn import _convo as C


@pytest.mark.parametrize("text", [
    "use a sub agent to check the logs",
    "Use subagents for the research part",
    "spawn an agent to read the jira ticket",
    "do the frontend in a separate agent",
    "run these agents in parallel",
    "delegate this to an agent",
])
def test_asks_are_detected(text):
    assert C.wants_subagent(text)


@pytest.mark.parametrize("text", [
    "fix the login bug", "the agent crashed yesterday", "user agent header is wrong",
])
def test_ordinary_requests_are_not(text):
    assert not C.wants_subagent(text)


def test_the_rule_names_spawn_task_and_the_default_defers_to_the_user():
    assert "spawn_task" in P.SUBAGENT_ASK_RULE and "Do not do that part" in P.SUBAGENT_ASK_RULE
    assert "when the user asks for a sub-agent" in P._MULTI_AGENT


def test_big_spawn_rule_names_spawn_task_and_keeps_integration():
    assert "spawn_task" in P.BIG_SPAWN_RULE and "full test run at the end" in P.BIG_SPAWN_RULE
