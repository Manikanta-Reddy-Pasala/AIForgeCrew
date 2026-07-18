"""Batch read_files: gather many files in ONE tool call so a local model never
needs a long, stall-prone one-at-a-time read chain."""
from __future__ import annotations

from aiforge_core.runtime import chat_agent
from aiforge_core.runtime.chat_agent._shell import _t_read_files


def test_registered_native_and_readonly():
    assert "read_files" in chat_agent.TOOLS
    assert "read_files" in chat_agent._READONLY_TOOLS      # usable in plan mode
    from aiforge_core.runtime.chat_agent._tools._schemas import NATIVE_TOOL_NAMES
    assert "read_files" in NATIVE_TOOL_NAMES               # callable with native args


def test_reads_many_files_in_one_call(tmp_path):
    for i in range(5):
        (tmp_path / f"m{i}.java").write_text(f"class M{i} {{ int f{i}; }}")
    r = _t_read_files({"paths": [f"m{i}.java" for i in range(5)]}, str(tmp_path))
    assert r["ok"] and r["read"] == 5 and r["failed"] == 0
    for i in range(5):
        assert f"=== m{i}.java ===" in r["content"]
        assert f"class M{i}" in r["content"]


def test_string_paths_and_missing_file(tmp_path):
    (tmp_path / "a.txt").write_text("alpha")
    # comma/newline string is accepted; a missing file is reported, not fatal
    r = _t_read_files({"paths": "a.txt, gone.txt"}, str(tmp_path))
    assert r["read"] == 1 and r["failed"] == 1
    assert "alpha" in r["content"]
    assert "[read failed:" in r["content"]


def test_per_file_cap_truncates(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_READ_FILES_PER_CAP", "300")
    (tmp_path / "big.txt").write_text("x" * 4000)
    r = _t_read_files({"paths": ["big.txt"]}, str(tmp_path))
    assert "…[truncated" in r["content"]
    assert r["content"].count("x") <= 360         # capped near 300, not 4000


def test_missing_paths_soft_error(tmp_path):
    assert _t_read_files({}, str(tmp_path))["ok"] is False
    assert _t_read_files({"paths": []}, str(tmp_path))["ok"] is False
