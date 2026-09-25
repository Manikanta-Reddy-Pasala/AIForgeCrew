"""Stop and a typed message cut a chat-agent wait short."""
import pytest

from aiforge_core.runtime import chat_cancel, chat_interject, run_interrupt


def test_pause_returns_as_soon_as_stop_is_set(monkeypatch):
    chat_cancel.start(77)
    try:
        monkeypatch.setattr(run_interrupt, "reason", lambda sid: "stop")
        assert run_interrupt.pause(30, 77) == "stop"
    finally:
        chat_cancel.finish(77)


def test_stop_wins_over_a_queued_message():
    chat_cancel.start(78)
    chat_interject.push(78, "tear")
    chat_cancel.cancel(78)
    try:
        assert run_interrupt.reason(78) == "stop"
    finally:
        chat_cancel.finish(78)
        chat_interject.clear(78)


def test_a_queued_message_is_a_steer_not_a_stop():
    chat_cancel.start(79)
    chat_interject.push(79, "tear")
    try:
        assert run_interrupt.reason(79) == "steer"
        assert run_interrupt.steered()["steered"] is True
    finally:
        chat_cancel.finish(79)
        chat_interject.clear(79)


def test_stop_skips_the_enhancer_model_call(monkeypatch):
    from aiforge_core.llm import client
    from aiforge_core.runtime.parallel_subtasks._planning_enhance import _enhance
    chat_cancel.start(80)
    chat_cancel.cancel(80)
    monkeypatch.setattr(client, "complete",
                        lambda *a, **k: pytest.fail("enhancer ran after Stop"))
    prompt = "please rewrite the login handler so empty passwords are rejected"
    try:
        assert _enhance(prompt, session_id=80) == prompt
    finally:
        chat_cancel.finish(80)
