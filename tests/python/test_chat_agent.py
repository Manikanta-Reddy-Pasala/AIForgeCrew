import json

import pytest

from aiforge_core.runtime import chat_agent as ca


def _scripted(outputs):
    """Return a complete_fn that yields the given outputs in order."""
    seq = list(outputs)

    def _fn(role, messages, **kw):
        return seq.pop(0)
    return _fn


def _collect(gen):
    return list(gen)


def test_final_immediately(tmp_path):
    fn = _scripted(["THOUGHT: easy\nFINAL: hello there"])
    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content": "hi"}], cwd=str(tmp_path), complete_fn=fn))
    assert evs[-1] == {"type": "done"}
    msg = [e for e in evs if e["type"] == "message"][0]
    assert msg["text"] == "hello there"


def test_file_write_then_final(tmp_path):
    fn = _scripted([
        'THOUGHT: write it\nACTION: file_write\nARGS_JSON: {"path": "a.txt", "content": "hi"}',
        "THOUGHT: done\nFINAL: wrote the file",
    ])
    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content": "make a.txt"}], cwd=str(tmp_path), complete_fn=fn))
    tool = [e for e in evs if e["type"] == "tool"][0]
    assert tool["name"] == "file_write"
    assert tool["result"]["ok"] is True
    assert (tmp_path / "a.txt").read_text() == "hi"
    assert [e for e in evs if e["type"] == "message"][0]["text"] == "wrote the file"


def test_file_read_roundtrip(tmp_path):
    (tmp_path / "b.txt").write_text("payload-xyz")
    fn = _scripted([
        'ACTION: file_read\nARGS_JSON: {"path": "b.txt"}',
        "FINAL: read it",
    ])
    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content": "read b"}], cwd=str(tmp_path), complete_fn=fn))
    tool = [e for e in evs if e["type"] == "tool"][0]
    assert tool["result"]["content"] == "payload-xyz"


def test_run_command(tmp_path):
    fn = _scripted([
        'ACTION: run_command\nARGS_JSON: {"cmd": "echo hello-cmd"}',
        "FINAL: ran",
    ])
    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content": "echo"}], cwd=str(tmp_path), complete_fn=fn))
    tool = [e for e in evs if e["type"] == "tool"][0]
    assert tool["result"]["ok"] is True
    assert "hello-cmd" in tool["result"]["stdout"]


def test_unknown_tool_reports_error(tmp_path):
    fn = _scripted([
        "ACTION: teleport\nARGS_JSON: {}",
        "FINAL: gave up",
    ])
    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content": "x"}], cwd=str(tmp_path), complete_fn=fn))
    tool = [e for e in evs if e["type"] == "tool"][0]
    assert tool["result"]["ok"] is False
    assert "unknown tool" in tool["result"]["error"]


def test_step_limit(tmp_path):
    # Always returns an action — never finishes; loop must cap.
    def _fn(role, messages, **kw):
        return 'ACTION: list_dir\nARGS_JSON: {"path": "."}'
    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content": "loop"}], cwd=str(tmp_path),
        complete_fn=_fn, max_steps=3))
    tools = [e for e in evs if e["type"] == "tool"]
    assert len(tools) == 3
    assert "step limit" in [e for e in evs if e["type"] == "message"][0]["text"]


def test_llm_error_is_soft(tmp_path):
    def _fn(role, messages, **kw):
        raise RuntimeError("boom")
    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content": "x"}], cwd=str(tmp_path), complete_fn=_fn))
    assert any(e["type"] == "error" for e in evs)
    assert evs[-1] == {"type": "done"}


def test_no_markers_treated_as_final(tmp_path):
    fn = _scripted(["just a plain answer with no protocol markers"])
    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content": "x"}], cwd=str(tmp_path), complete_fn=fn))
    assert [e for e in evs if e["type"] == "message"][0]["text"].startswith("just a plain")


def test_workspace_clamp(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_WORKSPACE_DIR", str(tmp_path))
    fn = _scripted([
        'ACTION: file_read\nARGS_JSON: {"path": "/etc/hosts"}',
        "FINAL: blocked",
    ])
    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content": "read outside"}], cwd=str(tmp_path),
        complete_fn=fn))
    tool = [e for e in evs if e["type"] == "tool"][0]
    assert tool["result"]["ok"] is False
    assert "escapes" in tool["result"]["error"]
