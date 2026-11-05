"""A reasoning model that fills the window's free part with thinking (no
answer, no call) makes its role condense earlier, and the step is asked
again with that room: 'This reply ran out of room before an answer was
written' on a multi-day office run after the condense point went from 50%
to 85% of the window."""
from __future__ import annotations

from aiforge_core.llm import reasoning
from aiforge_core.runtime.chat_agent import _native
from aiforge_core.runtime.chat_agent._context import _window as W


def test_a_reply_spent_on_reasoning_lowers_the_roles_condense_point(monkeypatch):
    from aiforge_core.llm import client
    monkeypatch.setattr(W, "_NEEDS_ROOM", set())
    monkeypatch.setattr(W, "_effective_window", lambda role=None: 131072)
    monkeypatch.delenv("AIFORGE_CTX_HISTORY_FRACTION", raising=False)
    monkeypatch.setattr(client, "complete_raw", lambda role, msgs, tools=None, tool_choice="auto": {
        "role": "assistant", "content": "", "reasoning_content": "hmm " * 50,
        "_finish_reason": "length"})
    monkeypatch.setattr(_native, "_model_for", lambda role: "m")
    before = W._history_fraction("doer")
    fn = _native.make_native_complete_fn(session_id=None)
    out = fn("doer", [{"role": "system", "content": "s"}, {"role": "user", "content": "do it"}])
    assert out == ""                                   # an empty step: asked again
    assert W._history_fraction("doer") == W._REASONING_FRACTION < before
    assert W._history_fraction("planner") == before   # only the role that ran out


def test_a_forced_final_stays_forced_for_the_retry(monkeypatch):
    from aiforge_core.llm import client
    monkeypatch.setattr(W, "_NEEDS_ROOM", set())
    monkeypatch.setattr(client, "complete_raw", lambda role, msgs, tools=None, tool_choice="auto": {
        "role": "assistant", "content": "", "_finish_reason": "length"})
    monkeypatch.setattr(_native, "_model_for", lambda role: "m")
    _native.force_final_next(91)
    fn = _native.make_native_complete_fn(session_id=91)
    fn("doer", [{"role": "system", "content": "s"}, {"role": "user", "content": "x"}])
    assert 91 in _native._FORCE_FINAL
    _native._FORCE_FINAL.discard(91)


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
