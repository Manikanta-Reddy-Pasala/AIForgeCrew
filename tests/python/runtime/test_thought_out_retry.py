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
        "_finish_reason": "length",
        "_usage": {"prompt_tokens": 120000, "completion_tokens": 11000}})   # window full
    monkeypatch.setattr(_native, "_model_for", lambda role: "m")
    monkeypatch.setattr(W, "_room_key", lambda role: (str(role), "m"))
    before = W._history_fraction("doer")
    fn = _native.make_native_complete_fn(session_id=None)
    out = fn("doer", [{"role": "system", "content": "s"}, {"role": "user", "content": "do it"}])
    assert out == ""                                   # an empty step: asked again
    assert W._history_fraction("doer") == W._REASONING_FRACTION < before
    assert W._history_fraction("planner") == before   # only the role that ran out


def test_a_forced_final_or_an_output_cap_is_re_posted_with_reasoning_off(monkeypatch):
    """No later step is guaranteed after a forced final (the deadline stops the
    turn), and condensing does not help an output cap: re-post now."""
    from aiforge_core.llm import client
    monkeypatch.setattr(W, "_NEEDS_ROOM", set())
    monkeypatch.setattr(W, "_effective_window", lambda role=None: 131072)
    seen = []

    def raw(role, msgs, tools=None, tool_choice="auto"):
        seen.append(reasoning._OFF.get())
        if len(seen) == 1:
            return {"role": "assistant", "content": "", "_finish_reason": "length",
                    "_usage": {"prompt_tokens": 30000, "completion_tokens": 4096}}  # a cap
        return {"role": "assistant", "content": "FINAL: summary", "_finish_reason": "stop"}
    monkeypatch.setattr(client, "complete_raw", raw)
    monkeypatch.setattr(_native, "_model_for", lambda role: "m")
    _native.force_final_next(91)
    fn = _native.make_native_complete_fn(session_id=91)
    out = fn("doer", [{"role": "system", "content": "s"}, {"role": "user", "content": "x"}])
    assert "summary" in out and seen == [False, True] and not W._NEEDS_ROOM


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


def test_a_forced_final_written_as_a_text_tool_call_is_asked_again_in_prose(monkeypatch):
    """click t5: the whole answer was ``<tool_call><function=run_command>…``."""
    from aiforge_core.llm import client
    asked = []

    def raw(role, msgs, tools=None, tool_choice="auto"):
        asked.append((tool_choice, msgs[-1]["content"]))
        if len(asked) == 1:
            return {"role": "assistant", "_finish_reason": "stop",
                    "content": "<tool_call>\n<function=run_command>\n<parameter=cmd>\npytest\n"
                               "</parameter>\n</function>\n</tool_call>"}
        return {"role": "assistant", "content": "FINAL: done A, B fails", "_finish_reason": "stop"}
    monkeypatch.setattr(client, "complete_raw", raw)
    monkeypatch.setattr(_native, "_model_for", lambda role: "m")
    _native.force_final_next(92)
    fn = _native.make_native_complete_fn(session_id=92)
    out = fn("doer", [{"role": "system", "content": "s"}, {"role": "user", "content": "x"}])
    assert "B fails" in out and "<tool_call>" not in out
    assert asked[1][0] == "none" and "plain prose" in asked[1][1]


def test_a_second_text_tool_call_has_the_markup_cut_out(monkeypatch):
    from aiforge_core.llm import client
    monkeypatch.setattr(client, "complete_raw", lambda role, msgs, tools=None, tool_choice="auto": {
        "role": "assistant", "_finish_reason": "stop",
        "content": "Summary: parts 1-3 done.\n<tool_call>\n<function=run_command>\n"
                   "<parameter=cmd>\nx\n"})
    monkeypatch.setattr(_native, "_model_for", lambda role: "m")
    _native.force_final_next(93)
    fn = _native.make_native_complete_fn(session_id=93)
    out = fn("doer", [{"role": "system", "content": "s"}, {"role": "user", "content": "x"}])
    assert "parts 1-3 done" in out and "<function=" not in out


def test_an_ordinary_step_with_text_markup_is_left_alone(monkeypatch):
    from aiforge_core.llm import client
    calls = []
    def raw(role, msgs, tools=None, tool_choice="auto"):
        calls.append(1)
        return {"role": "assistant", "_finish_reason": "stop", "content": "use <tool_call> tags"}
    monkeypatch.setattr(client, "complete_raw", raw)
    monkeypatch.setattr(_native, "_model_for", lambda role: "m")
    fn = _native.make_native_complete_fn(session_id=94)
    fn("doer", [{"role": "system", "content": "s"}, {"role": "user", "content": "x"}])
    assert calls == [1]


def test_a_final_that_quotes_the_markup_inline_is_kept_whole(monkeypatch):
    from aiforge_core.llm import client
    calls = []
    def raw(role, msgs, tools=None, tool_choice="auto"):
        calls.append(1)
        return {"role": "assistant", "_finish_reason": "stop",
                "content": "Done. The parser now handles `<tool_call>` blocks.\n\nStill failing: test_x."}
    monkeypatch.setattr(client, "complete_raw", raw)
    monkeypatch.setattr(_native, "_model_for", lambda role: "m")
    _native.force_final_next(95)
    fn = _native.make_native_complete_fn(session_id=95)
    out = fn("doer", [{"role": "system", "content": "s"}, {"role": "user", "content": "x"}])
    assert calls == [1] and "Still failing: test_x" in out


def test_a_final_that_is_only_calls_twice_says_so_not_the_reasoning(monkeypatch):
    from aiforge_core.llm import client
    monkeypatch.setattr(client, "complete_raw", lambda role, msgs, tools=None, tool_choice="auto": {
        "role": "assistant", "_finish_reason": "stop", "reasoning_content": "<tool_call>{}",
        "content": "<tool_call>\n<function=run_command>\n<parameter=cmd>\nls\n</parameter>\n"
                   "</function>\n</tool_call>"})
    monkeypatch.setattr(_native, "_model_for", lambda role: "m")
    _native.force_final_next(96)
    fn = _native.make_native_complete_fn(session_id=96)
    out = fn("doer", [{"role": "system", "content": "s"}, {"role": "user", "content": "x"}])
    assert "no final summary" in out and "<tool_call>" not in out


def test_markup_inside_a_think_block_is_not_a_call(monkeypatch):
    from aiforge_core.llm import client
    calls = []
    def raw(role, msgs, tools=None, tool_choice="auto"):
        calls.append(1)
        return {"role": "assistant", "_finish_reason": "stop",
                "content": "<think>\n<tool_call>\n<function=ls>\n<parameter=p>\n</think>\nAll done."}
    monkeypatch.setattr(client, "complete_raw", raw)
    monkeypatch.setattr(_native, "_model_for", lambda role: "m")
    _native.force_final_next(97)
    fn = _native.make_native_complete_fn(session_id=97)
    out = fn("doer", [{"role": "system", "content": "s"}, {"role": "user", "content": "x"}])
    assert calls == [1] and "All done." in out
