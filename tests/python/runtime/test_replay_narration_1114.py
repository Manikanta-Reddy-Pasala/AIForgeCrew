"""Replay keeps what the model said with each call (11-14).

The step text is ``ACTION/ARGS_JSON`` + a trailing ``THOUGHT:`` line. The
replay used to send ``content: None`` (the words were lost), and a THOUGHT
holding a ``}`` broke the greedy args match so the whole step went back as
plain text with no tool call."""
from __future__ import annotations

import json

import pytest

from aiforge_core.runtime.chat_agent._native import _synth_step
from aiforge_core.runtime.chat_agent._native_replay import to_native_messages


@pytest.fixture(autouse=True)
def _narration_on(monkeypatch):
    # off by default while it is measured; these tests are about what it does
    monkeypatch.setenv("AIFORGE_CHAT_REPLAY_NARRATION", "1")


def test_by_default_a_brace_in_the_thought_still_keeps_the_call(monkeypatch):
    monkeypatch.delenv("AIFORGE_CHAT_REPLAY_NARRATION")
    out = _replay(_step("file_patch", {"path": "a.py"}, said="fix {x: 1} here"))
    calls = [m for m in out if m.get("tool_calls")]
    assert calls and calls[0]["tool_calls"][0]["function"]["name"] == "file_patch"
    assert calls[0]["content"] is None                # narration off by default


def _step(name, args, said=""):
    msg = {"role": "assistant", "content": said,
           "tool_calls": [{"id": "x", "type": "function", "function": {
               "name": name, "arguments": json.dumps(args)}}]}
    return _synth_step(msg)


def _replay(step, result="OBSERVATION: ok"):
    return to_native_messages([
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": step},
        {"role": "user", "content": result},
    ])


def test_narration_rides_on_the_tool_call():
    out = _replay(_step("read_lines", {"path": "a.py"}, "The loop stops at `#`. Reading it."))
    call = out[1]
    assert call["tool_calls"][0]["function"]["name"] == "read_lines"
    assert call["content"] == "The loop stops at `#`. Reading it."
    assert out[2] == {"role": "tool", "tool_call_id": call["tool_calls"][0]["id"],
                      "name": "read_lines", "content": "ok"}


def test_narration_with_braces_keeps_the_call_and_its_args():
    said = "Fix: `if c == '}': break` and `d = {}` before the loop."
    args = {"path": "p.py", "old_text": "x = {\n  1: 2}\n", "new_text": "y = {}"}
    out = _replay(_step("file_patch", args, said))
    call = out[1]
    assert call["tool_calls"], "step must stay a tool call"
    assert json.loads(call["tool_calls"][0]["function"]["arguments"]) == args
    assert "if c == '}': break" in call["content"]
    assert out[2]["role"] == "tool"


def test_no_narration_sends_no_content():
    out = _replay(_step("grep", {"pattern": "x"}))
    assert out[1]["content"] is None


def test_text_protocol_thought_before_action_is_kept():
    step = 'THOUGHT: look at the parser first\nACTION: read_lines\nARGS_JSON: {"path": "p.py"}'
    out = _replay(step)
    assert out[1]["content"] == "look at the parser first"
    assert json.loads(out[1]["tool_calls"][0]["function"]["arguments"]) == {"path": "p.py"}


def test_switch_off_drops_narration(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_REPLAY_NARRATION", "0")
    out = _replay(_step("grep", {"pattern": "x"}, "searching"))
    assert out[1]["content"] is None and out[1]["tool_calls"]


def test_broken_args_stay_plain_text():
    step = 'ACTION: grep\nARGS_JSON: {"pattern": '
    out = _replay(step)
    assert out[1] == {"role": "assistant", "content": step}
