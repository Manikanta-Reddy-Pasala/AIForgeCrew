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


def test_tool_start_precedes_tool_with_matching_call_id(tmp_path):
    """A slow tool used to show NOTHING until it finished — `tool_start`
    fires first (same name/args) so the UI can show 'running…' immediately,
    and its call_id matches the completed `tool` event so the UI can flip
    the same row in place instead of showing two rows."""
    fn = _scripted([
        'ACTION: file_write\nARGS_JSON: {"path": "a.txt", "content": "hi"}',
        "FINAL: wrote the file",
    ])
    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content": "make a.txt"}], cwd=str(tmp_path), complete_fn=fn))
    start = [e for e in evs if e["type"] == "tool_start"][0]
    tool = [e for e in evs if e["type"] == "tool"][0]
    assert start["name"] == tool["name"] == "file_write"
    assert start["args"] == tool["args"]
    assert start["call_id"] == tool["call_id"]
    assert evs.index(start) < evs.index(tool)


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


def test_run_command_preflight_missing_cd_and_script(tmp_path):
    """A literal `cd <missing>` or `bash <missing.sh>` is refused with an
    actionable error BEFORE the shell runs (no cryptic 'No such file')."""
    repo = str(tmp_path)
    r = ca._t_run_command({"cmd": "cd no/such/dir && ls"}, repo)
    assert r["blocked"] == "missing_path" and "cd target" in r["error"]
    r = ca._t_run_command({"cmd": "bash deploy.sh"}, repo)
    assert r["blocked"] == "missing_path" and "script" in r["error"]
    r = ca._t_run_command({"cmd": "./go.sh --fast"}, repo)
    assert r["blocked"] == "missing_path"
    # `cd` chain tracked: script checked under the cd-ed dir.
    (tmp_path / "sub").mkdir()
    r = ca._t_run_command({"cmd": "cd sub && python x.py"}, repo)
    assert r["blocked"] == "missing_path" and "x.py" in r["error"]
    # existing paths run fine… (use `sh`, not `python`: a bare `python` is
    # absent on modern macOS/CI hosts, which fails the RUN, not the preflight)
    (tmp_path / "sub" / "run.sh").write_text("echo hi\n")
    r = ca._t_run_command({"cmd": "cd sub && sh run.sh"}, repo)
    assert r.get("ok") is True and "hi" in r["stdout"]
    # …and dynamic paths fail OPEN (no false block on $VARs / substitution).
    r = ca._t_run_command({"cmd": "cd $HOME && echo ok"}, repo)
    assert r.get("blocked") != "missing_path"


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


def test_memory_write_tool(tmp_path, monkeypatch):
    # embedded memory → writes land in sqlite_memory
    import importlib
    for k in ("AIFORGE_MEMORY_BACKEND", "NEO4J_URI", "AIFORGE_NEO4J_URI",
              "AIFORGE_PG_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "mem.db"))
    import aiforge_core.memory.backend_select as bs; importlib.reload(bs)
    import aiforge_core.memory.sqlite_memory as sm; importlib.reload(sm)
    fn = _scripted([
        'ACTION: memory_write\nARGS_JSON: {"text": "staging needs the VPN on first", "kind": "gotcha"}',
        "FINAL: saved",
    ])
    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content": "remember this"}], cwd=str(tmp_path / "myrepo"),
        complete_fn=fn))
    tool = [e for e in evs if e["type"] == "tool"][0]
    assert tool["name"] == "memory_write"
    assert tool["result"]["ok"] is True
    assert sm.stats()["total"] == 1


def test_fenced_args_write_persists(tmp_path):
    # model wraps args in a ```json fence — must still extract + write
    fn = _scripted([
        'ACTION: file_write\nARGS_JSON:\n```json\n{"path": "out.txt", "content": "BANANA"}\n```',
        "FINAL: wrote it",
    ])
    evs = _collect(ca.run_chat_agent(
        [{"role": "user", "content": "write it"}], cwd=str(tmp_path), complete_fn=fn))
    tool = [e for e in evs if e["type"] == "tool"][0]
    assert tool["result"]["ok"] is True
    assert (tmp_path / "out.txt").read_text() == "BANANA"
