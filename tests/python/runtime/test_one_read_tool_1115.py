"""One OpenCode-shaped read tool (AIFORGE_CHAT_ONE_READ=1)."""
from __future__ import annotations

from aiforge_core.runtime.chat_agent._tools import _schemas as S
from aiforge_core.runtime.chat_agent._tools._code import _t_read


def _names(schemas):
    return {s["function"]["name"] for s in schemas}


def test_read_returns_numbered_lines_from_offset(tmp_path):
    f = tmp_path / "big.py"
    f.write_text("".join(f"line {i}\n" for i in range(1, 5001)))
    out = _t_read({"path": str(f), "offset": 100, "limit": 3}, str(tmp_path))
    text = str(out)
    assert out["ok"] and "line 101" in text and "line 103" in text and "line 104" not in text
    whole = _t_read({"filePath": str(f)}, str(tmp_path))
    assert "line 2000" in str(whole) and "line 2001" not in str(whole)      # default 2000, no outline


def test_the_switch_swaps_the_three_read_tools_for_one(monkeypatch):
    schemas = S.NATIVE_TOOL_SCHEMAS
    off = _names(S.filter_native(schemas, mode="act", text="fix the bug"))
    assert {"file_read", "read_lines"} <= off and "read" not in off
    monkeypatch.setenv("AIFORGE_CHAT_ONE_READ", "1")
    on = _names(S.filter_native(schemas, mode="act", text="fix the bug"))
    assert "read" in on and not on & {"file_read", "read_files", "read_lines"}
    ro = _names(S.filter_native(schemas, mode="plan", text="explain the parser"))
    assert "read" in ro
