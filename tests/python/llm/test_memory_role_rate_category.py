"""Memory work must keep the idle allowance it had as `learner`.

The compaction category's ceiling defaults to 0 — bounded only by the global
window — so a fold uses whatever chat is not using, which on an idle box is
everything. Moving distillation to its own `memory` role without listing it
here put every fold on the 15 rpm INTERACTIVE ceiling instead, and compaction
crawled on a box nobody was using.
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


def test_the_compaction_ceiling_is_the_whole_window_by_default(monkeypatch):
    for var in ("AIFORGE_COMPACTION_RPM", "AIFORGE_CHAT_RPM"):
        monkeypatch.delenv(var, raising=False)
    assert rl._cat_rpm("compaction") == 0.0      # 0 = only the global window
    assert rl._cat_rpm("chat") > 0
