"""Quick mode: one doer, a hard step cap, for asks that should not take minutes.

The chat agent normally runs until it decides it is finished — a stuck-loop
detector bounds it, not a step count. That is right for real work and wrong for
"rename this variable", where the agent's own exploration costs more than the
change. Quick mode is the user-facing lever for that trade.
"""
from __future__ import annotations

import pytest

from aiforge_core.api.routes import chat as chat_routes


def test_quick_off_leaves_the_loop_open():
    """None = no cap: the normal, thorough behaviour must be untouched."""
    assert chat_routes._quick_step_cap(False) is None


def test_quick_on_caps_the_steps():
    assert chat_routes._quick_step_cap(True) == 6


def test_the_cap_is_tunable(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_QUICK_STEPS", "3")
    assert chat_routes._quick_step_cap(True) == 3


def test_a_garbage_cap_falls_back_rather_than_raising(monkeypatch):
    """This is read on the request path — a typo in the env must not 500 a chat
    turn."""
    monkeypatch.setenv("AIFORGE_CHAT_QUICK_STEPS", "fast please")
    assert chat_routes._quick_step_cap(True) == 6


def test_the_cap_is_at_least_one_step(monkeypatch):
    """A zero cap would mean an agent that cannot act at all — worse than slow."""
    monkeypatch.setenv("AIFORGE_CHAT_QUICK_STEPS", "0")
    assert chat_routes._quick_step_cap(True) == 1


def test_the_request_model_defaults_to_off():
    """Quick changes how thoroughly the agent works, so it is opt-in: a client
    that knows nothing about it keeps today's behaviour."""
    body = chat_routes._SessionMsgBody(content="hello")
    assert body.quick is False


def test_the_request_model_accepts_the_flag():
    body = chat_routes._SessionMsgBody(content="rename that var", quick=True)
    assert body.quick is True
