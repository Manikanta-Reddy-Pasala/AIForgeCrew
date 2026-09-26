"""Compaction's rate cap follows the model server's slots.

One slot: an in-flight fold sits ahead of the person's next message, so the
cap holds even on an idle box — including the turn's own condense summary.
Several slots: the idle lift applies as before, and the condense summary runs
beside the chat with no category cap.
"""
from __future__ import annotations

import pytest

from aiforge_core.llm import interactive_gate as gate
from aiforge_core.llm import rate_limiter as rl
from aiforge_core.llm import slots


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_LLM_SHARED_WINDOW", "0")
    for var in ("AIFORGE_LLM_MAX_RPM", "AIFORGE_CHAT_RPM",
                "AIFORGE_COMPACTION_RPM", "AIFORGE_LLM_PARALLEL"):
        monkeypatch.delenv(var, raising=False)
    from aiforge_core.config import _filecache
    _filecache.clear()
    slots.reset()
    gate.reset()
    rl.reset_global()
    yield
    gate.set_exempt(False)
    gate.reset()
    rl.reset_global()


def _slots(monkeypatch, n):
    monkeypatch.setenv("AIFORGE_LLM_PARALLEL", str(n))


def test_one_slot_keeps_the_cap_on_an_idle_box(monkeypatch):
    _slots(monkeypatch, 1)
    assert rl._category_limit("compaction") == 5
    from aiforge_core.config import runtime_settings as rs
    rs.set_many({"llm_max_rpm": 40})
    assert rl._category_limit("compaction") == 5


def test_one_slot_caps_the_turn_summary_too(monkeypatch):
    _slots(monkeypatch, 1)
    gate.set_exempt(True)
    assert rl._category_limit("compaction") == 5


def test_unknown_slots_is_one(monkeypatch):
    # The test env points every endpoint at a closed port: the probe says 1.
    assert slots.llm_slots("learner") == 1
    assert rl._category_limit("compaction") == 5


def test_several_slots_lift_the_idle_cap(monkeypatch):
    _slots(monkeypatch, 4)
    assert rl._category_limit("compaction") == 0          # idle, no global
    gate.note_interactive()
    assert rl._category_limit("compaction") == 5          # chat active


def test_several_slots_turn_summary_runs_beside_chat(monkeypatch):
    _slots(monkeypatch, 4)
    gate.note_interactive()                               # chat is active
    gate.set_exempt(True)                                 # the condense summary
    assert rl._category_limit("compaction") == 0
    assert rl.acquire_global(role="learner", max_wait_s=1) == 0.0


def test_chat_category_is_never_touched(monkeypatch):
    _slots(monkeypatch, 1)
    assert rl._category_limit("chat") == 0
