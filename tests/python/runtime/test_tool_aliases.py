"""Other agents' tool names run the tool they mean: a click bench run called
``read_file`` 40 times and every call failed as an unknown tool."""
from __future__ import annotations

import pytest

from aiforge_core.runtime.chat_agent._registry import TOOLS
from aiforge_core.runtime.chat_agent._tools._aliases import resolve


@pytest.mark.parametrize("name,args,want", [
    ("read_file", {"file_path": "a.py", "offset": 3, "limit": 2},
     ("file_read", {"path": "a.py", "offset": 3, "limit": 2})),
    ("write", {"filePath": "a.py", "content": "x"}, ("file_write", {"path": "a.py", "content": "x"})),
    ("edit", {"filePath": "a.py", "oldString": "a", "newString": "b"},
     ("file_patch", {"path": "a.py", "old_str": "a", "new_str": "b"})),
    ("bash", {"command": "ls"}, ("run_command", {"cmd": "ls"})),
    ("read_file", {"path": "a.py", "file_path": "b.py"},          # the tool's own name wins
     ("file_read", {"path": "a.py"})),
    ("file_read", {"file_path": "a.py"}, ("file_read", {"file_path": "a.py"})),   # a real tool
    ("frobnicate", {"x": 1}, ("frobnicate", {"x": 1})),                          # still unknown
])
def test_an_other_agents_tool_name_runs_the_tool_it_means(name, args, want):
    tool, out = resolve(name, args, TOOLS)
    assert (tool, {k: v for k, v in out.items() if k in want[1]}) == want
    assert set(out) == set(want[1])


def test_a_read_file_call_reads_the_file(tmp_path):
    (tmp_path / "a.py").write_text("one\ntwo\nthree\n")
    tool, args = resolve("read_file", {"file_path": "a.py", "offset": 2, "limit": 1}, TOOLS)
    res = TOOLS[tool](args, str(tmp_path))
    assert res["ok"] and res["text"].startswith("two")


def test_every_aliased_call_of_one_native_reply_is_batched_under_its_real_name():
    from aiforge_core.runtime.chat_agent import _native
    msg = {"tool_calls": [
        {"function": {"name": "read_file", "arguments": '{"file_path": "a.py"}'}},
        {"function": {"name": "read_file", "arguments": '{"file_path": "b.py"}'}},
        {"function": {"name": "read_file", "arguments": '{"file_path": "c.py"}'}}]}
    assert _native._synth_step(msg).startswith("ACTION: file_read")
    steps, skipped = _native._queued_steps(msg)
    assert skipped == 0 and len(steps) == 2 and all("ACTION: file_read" in s for s in steps)


def test_a_bash_timeout_in_milliseconds_becomes_seconds():
    assert resolve("bash", {"command": "make", "timeout": 120000}, TOOLS)[1]["timeout"] == 120


def test_a_bash_call_in_run_commands_own_shape_keeps_its_seconds():
    assert resolve("bash", {"cmd": "make", "timeout": 1800}, TOOLS)[1]["timeout"] == 1800
