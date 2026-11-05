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
