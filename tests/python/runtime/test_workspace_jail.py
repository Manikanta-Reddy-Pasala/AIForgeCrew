"""Workspace jail: a chat may not WRITE outside its own cwd.

The cwd a session runs in was only a default — an absolute path handed to a
mutating file tool wrote wherever it pointed, which is how a chat that merely
READ about another repo (in recall) went on to edit it. Those writes are now
refused before they happen. ON by default; ``AIFORGE_CHAT_WORKSPACE_JAIL=0``
opts a session out.

Reads are deliberately NOT jailed: looking at another repo is useful, editing
it unasked is the bug.
"""
import os

from aiforge_core.runtime import scope_guard


def _args(path):
    return {"path": path, "content": "x"}


def test_on_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_CHAT_WORKSPACE_JAIL", raising=False)
    assert scope_guard.workspace_jail_on() is True
    assert scope_guard.outside_workspace(
        "file_write", _args("/somewhere/else/x.py"), str(tmp_path)) \
        == ["/somewhere/else/x.py"]


def test_explicit_opt_out(monkeypatch, tmp_path):
    """A session that legitimately writes outside its cwd can turn it off."""
    for off in ("0", "false", "no", "off"):
        monkeypatch.setenv("AIFORGE_CHAT_WORKSPACE_JAIL", off)
        assert scope_guard.workspace_jail_on() is False, off
        assert scope_guard.outside_workspace(
            "file_write", _args("/somewhere/else/x.py"), str(tmp_path)) == []


def test_empty_value_is_not_off(monkeypatch, tmp_path):
    """A wrapper that clears the var must not silently drop the guard."""
    monkeypatch.setenv("AIFORGE_CHAT_WORKSPACE_JAIL", "")
    assert scope_guard.workspace_jail_on() is True


def test_blocks_absolute_path_outside(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CHAT_WORKSPACE_JAIL", "1")
    ws = tmp_path / "session-73"
    ws.mkdir()
    other = tmp_path / "sys-gpsd" / "ublox_verify.py"
    other.parent.mkdir()
    blocked = scope_guard.outside_workspace(
        "file_write", _args(str(other)), str(ws))
    assert blocked == [str(other)]


def test_allows_paths_inside(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CHAT_WORKSPACE_JAIL", "1")
    ws = tmp_path / "session-73"
    (ws / "sub").mkdir(parents=True)
    assert scope_guard.outside_workspace(
        "file_write", _args("notes.md"), str(ws)) == []
    assert scope_guard.outside_workspace(
        "file_write", _args(str(ws / "sub" / "a.py")), str(ws)) == []


def test_blocks_traversal_escape(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CHAT_WORKSPACE_JAIL", "1")
    ws = tmp_path / "session-73"
    ws.mkdir()
    assert scope_guard.outside_workspace(
        "file_write", _args("../escape.py"), str(ws)) == ["../escape.py"]


def test_blocks_symlink_pointing_out(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CHAT_WORKSPACE_JAIL", "1")
    ws = tmp_path / "session-73"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = ws / "link"
    os.symlink(str(outside), str(link))
    assert scope_guard.outside_workspace(
        "file_write", _args(str(link / "x.py")), str(ws)) == [str(link / "x.py")]


def test_read_tools_are_not_jailed(monkeypatch, tmp_path):
    """Only the mutating file tools carry path extractors — a read elsewhere
    stays allowed."""
    monkeypatch.setenv("AIFORGE_CHAT_WORKSPACE_JAIL", "1")
    ws = tmp_path / "session-73"
    ws.mkdir()
    assert scope_guard.outside_workspace(
        "read", {"path": "/etc/hosts"}, str(ws)) == []
    assert scope_guard.outside_workspace(
        "grep", {"path": "/other/repo"}, str(ws)) == []


def test_no_cwd_allows(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_WORKSPACE_JAIL", "1")
    assert scope_guard.outside_workspace(
        "file_write", _args("/anywhere/x.py"), None) == []


# ── the dispatch gate actually refuses ─────────────────────────────────────

def _drive(gen):
    """Run a _pre_tool_checks generator to completion → (events, return)."""
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        return events, stop.value


def test_pre_tool_checks_refuses_the_write(monkeypatch, tmp_path):
    import types

    from aiforge_core.runtime.chat_agent import _loop
    monkeypatch.setenv("AIFORGE_CHAT_WORKSPACE_JAIL", "1")
    ws = tmp_path / "session-73"
    ws.mkdir()
    target = str(tmp_path / "sys-gpsd" / "ublox_verify.py")
    st = types.SimpleNamespace(convo=[])

    events, ret = _drive(_loop._pre_tool_checks(
        st, "file_write", {"path": target, "content": "x"}, str(ws), None))

    assert ret == "continue"                     # dispatch is skipped
    assert len(events) == 1
    result = events[0]["result"]
    assert result["ok"] is False
    assert result["error"] == "outside_workspace"
    assert result["blocked_paths"] == [target]
    # The model is told why, in the transcript, so it can correct itself.
    assert "outside_workspace" in st.convo[-1]["content"]


def test_pre_tool_checks_lets_the_write_through_when_inside(monkeypatch, tmp_path):
    import types

    from aiforge_core.runtime.chat_agent import _loop
    monkeypatch.setenv("AIFORGE_CHAT_WORKSPACE_JAIL", "1")
    ws = tmp_path / "session-73"
    ws.mkdir()
    st = types.SimpleNamespace(convo=[])

    events, ret = _drive(_loop._pre_tool_checks(
        st, "file_write", {"path": "notes.md", "content": "x"}, str(ws), None))

    assert events == []
    assert ret is None                           # no block → normal dispatch
