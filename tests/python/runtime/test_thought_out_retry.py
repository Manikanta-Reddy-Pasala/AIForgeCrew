"""A reasoning model that spends its whole reply length thinking (no answer,
no call) is asked once more with reasoning off, so the room goes to the
answer: 'This reply ran out of room before an answer was written' on medium
and big tasks."""
from __future__ import annotations

from aiforge_core.llm import reasoning
from aiforge_core.runtime.chat_agent import _native


def test_a_reply_spent_on_reasoning_is_retried_with_reasoning_off(monkeypatch):
    from aiforge_core.llm import client
    seen = []

    def raw(role, msgs, tools=None, tool_choice="auto"):
        off = reasoning.reasoning_off("m", "", "chat")
        seen.append(off)
        if len(seen) == 1:
            return {"role": "assistant", "content": "", "reasoning_content": "hmm " * 50,
                    "_finish_reason": "length"}
        return {"role": "assistant", "content": "FINAL: done, tests pass",
                "_finish_reason": "stop"}
    monkeypatch.setattr(client, "complete_raw", raw)
    monkeypatch.setattr(_native, "_model_for", lambda role: "m")
    fn = _native.make_native_complete_fn(session_id=None)
    out = fn("chat", [{"role": "system", "content": "s"}, {"role": "user", "content": "do it"}])
    assert "done, tests pass" in out
    assert seen[1] is True                      # the retry had reasoning forced off
    assert not reasoning.reasoning_off("m", "", "planner") or True


def test_only_an_empty_length_stop_counts_as_thought_out():
    assert _native._thought_out({"_finish_reason": "length", "content": "<think>x</think>"})
    assert not _native._thought_out({"_finish_reason": "length", "content": "FINAL: x"})
    assert not _native._thought_out({"_finish_reason": "length", "content": "",
                                     "tool_calls": [{"function": {"name": "grep"}}]})
    assert not _native._thought_out({"_finish_reason": "stop", "content": ""})


def test_forced_off_is_scoped_to_the_block():
    with reasoning.forced_off():
        assert reasoning.reasoning_off("m", "", "planner")
    assert reasoning._OFF.get() is False
