"""A suggestion is SENT as the next chat message, so it has to read like one.

The chip hands its sentence straight back to the agent with no memory of the
turn attached. "Re-run them with the fix" then arrives as a riddle and costs a
round trip; a hedge ("maybe consider …") arrives as a question, not an action.
"""
from __future__ import annotations

import pytest

from aiforge_core.runtime.next_step import _predict


@pytest.mark.parametrize("raw,want", [
    ("Please run pytest tests/python", "run pytest tests/python"),
    ("maybe consider adding a test to _render.py", "adding a test to _render.py"),
    ("You could update run.sh now", "update run.sh now"),
    ("I can rerun scripts/build.sh", "rerun scripts/build.sh"),
])
def test_the_hedge_opener_is_stripped(raw, want):
    assert _predict.tidy_action(raw) == want


def test_the_prompt_asks_for_a_standalone_command():
    assert "SENT BACK AS THE NEXT CHAT MESSAGE" in _predict._SYS
    assert "under 15 words" in _predict._SYS


def test_a_terse_suggestion_still_reaches_the_user(monkeypatch):
    """The wording fix is the PROMPT, not a filter. A short action like "check
    it" is short because the turn it follows supplies the subject — an earlier
    "names no target" gate dropped exactly those and emptied the feature."""
    from aiforge_core.runtime import next_step
    monkeypatch.setattr(
        _predict, "raw_prediction",
        lambda _ctx: {"id": "p-1", "action": "check it", "tool": "run_command",
                      "args": {}, "confidence": 0.99, "rationale": ""})
    p = next_step.predict({"message": "is the parser wired up?",
                           "did": "wired it", "repo": "/repo"})
    assert p is not None
    assert p.action == "check it"
