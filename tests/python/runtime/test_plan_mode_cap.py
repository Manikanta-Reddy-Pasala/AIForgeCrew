"""Plan/design mode: always step-capped; plan_ready only for a real plan."""
from __future__ import annotations

from aiforge_core.api.routes._chat import _routing as R


def test_plan_step_cap_defaults_to_eight(monkeypatch):
    monkeypatch.delenv("AIFORGE_CHAT_PLAN_STEPS", raising=False)
    assert R._plan_step_cap(False) == 8
    monkeypatch.setenv("AIFORGE_CHAT_PLAN_STEPS", "5")
    assert R._plan_step_cap(False) == 5
    assert R._plan_step_cap(True) == 6  # quick uses quick cap


def test_looks_like_plan_accepts_structured_final():
    text = ("FINAL:\n## Goal\nAdd fizzbuzz\n## Steps\n1. edit a.py\n"
            "## Files\na.py\n## Commands / tests\npytest\n## Risks\nnone")
    assert R._looks_like_plan(text) is True


def test_looks_like_plan_rejects_ask_and_short():
    assert R._looks_like_plan("ASK: which framework?") is False
    assert R._looks_like_plan("ok") is False
    assert R._looks_like_plan("") is False
