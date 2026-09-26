"""Memory work stays on the compaction category, not the chat ceiling.

The stored compaction cap defaults to 5 while chat is sending. With no chat
send in the minute, the limiter raises that to the global ceiling. Moving
distillation to its own `memory` role without listing it here put every fold
on the interactive ceiling instead.
"""
from __future__ import annotations

import pytest

from aiforge_core.llm import rate_limiter as rl


@pytest.mark.parametrize("role", ["learner", "memory"])
def test_a_memory_role_counts_as_compaction(role):
    assert rl._category(role) == "compaction"


@pytest.mark.parametrize("role", ["doer", "planner", "chat", None])
def test_an_interactive_role_still_counts_as_chat(role):
    assert rl._category(role) == "chat"


def test_an_overridden_memory_role_keeps_the_allowance(monkeypatch):
    """AIFORGE_MEMORY_MODEL_ROLE is a supported override; pointing memory work
    at another role must not quietly demote it to the chat ceiling."""
    monkeypatch.setenv("AIFORGE_MEMORY_MODEL_ROLE", "refiner")
    assert rl._category("refiner") == "compaction"
    assert rl._category("learner") == "compaction"
    assert rl._category("doer") == "chat"


def test_the_compaction_ceiling_is_five_while_chat_is_sending(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    from aiforge_core.config import _filecache
    _filecache.clear()
    for var in ("AIFORGE_COMPACTION_RPM", "AIFORGE_CHAT_RPM",
                "AIFORGE_LLM_MAX_RPM"):
        monkeypatch.delenv(var, raising=False)
    assert rl._cat_rpm("compaction") == 5.0
    assert rl._cat_rpm("chat") == 30.0
