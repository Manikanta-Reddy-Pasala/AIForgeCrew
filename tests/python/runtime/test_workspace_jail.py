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


# ── a folder the user named is consent ─────────────────────────────────────
# "put validate.py in /home/me/code/proj" was refused as outside_workspace, and
# the agent then wrote it anyway with `cat >` / `mv` — three wasted steps and no
# protection. The jail's job is repos the user never brought into the chat.

def test_user_named_folder_and_file_parent_become_roots(tmp_path):
    proj = tmp_path / "code" / "proj"
    proj.mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    roots = scope_guard.user_named_roots([
        f"create validate.py and put the file in the folder {proj}.",
        f"also write {other}/new_file.txt please",
        "and/or see http://example.com/a/b and /api/health",
    ])
    assert roots == [os.path.realpath(proj), os.path.realpath(other)]


def test_a_user_named_folder_is_writable(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CHAT_WORKSPACE_JAIL", "1")
    ws = tmp_path / "session-1"
    ws.mkdir()
    proj = tmp_path / "code" / "proj"
    proj.mkdir(parents=True)
    roots = scope_guard.user_named_roots([f"put it in {proj}"])
    assert scope_guard.outside_workspace(
        "file_write", _args(str(proj / "validate.py")), str(ws), roots) == []
    # a sibling the user did NOT name stays blocked
    sib = str(tmp_path / "code" / "elsewhere" / "x.py")
    assert scope_guard.outside_workspace(
        "file_write", _args(sib), str(ws), roots) == [sib]


def test_the_loop_takes_roots_from_user_turns_only(monkeypatch, tmp_path):
    """Recall and tool output are not consent: only role=user text counts."""
    from aiforge_core.runtime.chat_agent import _loop
    proj = tmp_path / "proj"
    proj.mkdir()
    recalled = tmp_path / "recalled"
    recalled.mkdir()
    st = _loop._build_loop_state(
        [{"role": "assistant", "content": f"I remember {recalled}"},
         {"role": "user", "content": f"write it in {proj}"}],
        str(tmp_path / "ws"), "chat", 3, lambda *a, **k: "FINAL: x",
        None, "act", None, None, False)
    assert st.user_roots == [os.path.realpath(proj)]


def test_refusal_lists_the_allowed_folders_and_forbids_the_shell_route(monkeypatch, tmp_path):
    import types

    from aiforge_core.runtime.chat_agent import _loop
    monkeypatch.setenv("AIFORGE_CHAT_WORKSPACE_JAIL", "1")
    ws = tmp_path / "session-1"
    ws.mkdir()
    named = tmp_path / "named"
    named.mkdir()
    st = types.SimpleNamespace(convo=[], user_roots=[str(named)])
    events, ret = _drive(_loop._pre_tool_checks(
        st, "file_write", {"path": str(tmp_path / "x" / "a.py"), "content": "x"},
        str(ws), None))
    result = events[0]["result"]
    assert result["allowed_folders"] == [str(ws), str(named)]
    assert "Do NOT write there another way" in result["hint"]


# ── interactive: the jail ASKS, one click grants the folder ────────────────
# It used to refuse and leave the agent to talk the user round — down to
# telling them to set AIFORGE_CHAT_WORKSPACE_JAIL=0, an operator env var.

def _interactive(monkeypatch, tmp_path, decision):
    import types

    from aiforge_core.runtime import chat_approve
    from aiforge_core.runtime.chat_agent import _loop
    monkeypatch.setenv("AIFORGE_CHAT_WORKSPACE_JAIL", "1")
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(chat_approve, "request", lambda sid: 7)
    monkeypatch.setattr(chat_approve, "wait", lambda sid: decision)
    ws = tmp_path / "session-1"
    ws.mkdir()
    repo = tmp_path / "code" / "mission-support"
    (repo / ".git").mkdir(parents=True)
    (repo / "pkg" / "schemas").mkdir(parents=True)
    target = str(repo / "pkg" / "schemas" / "a.json")
    st = types.SimpleNamespace(convo=[], user_roots=[], session_id=41)
    events, ret = _drive(_loop._pre_tool_checks(
        st, "file_write", {"path": target, "content": "x"}, str(ws), None))
    return st, events, ret, repo


def test_an_outside_write_asks_and_approval_grants_the_repo(monkeypatch, tmp_path):
    from aiforge_core.runtime import chat_write_grants
    st, events, ret, repo = _interactive(
        monkeypatch, tmp_path, {"decision": "approve"})
    assert events[0]["type"] == "approval"
    assert events[0]["grant_roots"] == [os.path.realpath(repo)]
    assert "AIFORGE_" not in events[0]["reason"]
    assert ret is None                               # the write goes ahead
    assert st.user_roots == [os.path.realpath(repo)]
    # remembered for the rest of the chat (next turns rebuild user_roots)
    assert chat_write_grants.granted(41) == [os.path.realpath(repo)]
    assert chat_write_grants.granted(42) == []       # per session, not global


def test_a_rejected_grant_does_not_write(monkeypatch, tmp_path):
    from aiforge_core.runtime import chat_write_grants
    st, events, ret, _repo = _interactive(
        monkeypatch, tmp_path, {"decision": "reject"})
    assert ret == "return"                           # stop and wait for the user
    assert events[-1]["type"] == "done"
    assert chat_write_grants.granted(41) == []


def test_granted_folders_join_the_next_turns_roots(monkeypatch, tmp_path):
    from aiforge_core.runtime import chat_write_grants
    from aiforge_core.runtime.chat_agent import _loop
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    proj = tmp_path / "proj"
    proj.mkdir()
    chat_write_grants.grant(9, [str(proj)])
    st = _loop._build_loop_state(
        [{"role": "user", "content": "carry on"}],
        str(tmp_path / "ws"), "chat", 3, lambda *a, **k: "FINAL: x",
        9, "act", None, None, False)
    assert str(proj) in st.user_roots


def test_grant_root_is_the_repo_or_nearest_folder_never_home(tmp_path, monkeypatch):
    from aiforge_core.runtime import chat_write_grants as g
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "code" / "r"
    (repo / ".git").mkdir(parents=True)
    (repo / "src").mkdir()
    assert g.grant_root(str(repo / "src" / "new" / "x.py")) == os.path.realpath(repo)
    plain = tmp_path / "plain"
    plain.mkdir()
    assert g.grant_root(str(plain / "x.py")) == os.path.realpath(plain)
    # a new folder straight under ~ is granted by itself, not ~
    assert g.grant_root(str(tmp_path / "newproj" / "x.py")) \
        == os.path.realpath(tmp_path / "newproj")


def test_deleting_chats_drops_their_grants(monkeypatch, tmp_path):
    from aiforge_core.runtime import chat_write_grants as g
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    g.grant(1, ["/a"])
    g.grant(2, ["/b"])
    g.forget(1)
    assert g.granted(1) == []
    assert g.granted(2) == ["/b"]
    g.forget_all()
    assert g.granted(2) == []


def test_a_shell_write_outside_asks_too(monkeypatch, tmp_path):
    """run_command with `cat > /elsewhere` was the agent's way around a refused
    file_write; in a chat it now gets the same Allow prompt."""
    import types

    from aiforge_core.runtime import chat_approve, shell_writes
    from aiforge_core.runtime.chat_agent import _loop
    monkeypatch.setenv("AIFORGE_CHAT_WORKSPACE_JAIL", "1")
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(shell_writes, "_temp_roots", lambda: ())  # tmp_path is /tmp
    monkeypatch.setattr(chat_approve, "request", lambda sid: 3)
    monkeypatch.setattr(chat_approve, "wait", lambda sid: {"decision": "reject"})
    ws = tmp_path / "session-1"
    ws.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    st = types.SimpleNamespace(convo=[], user_roots=[], session_id=5)
    events, ret = _drive(_loop._pre_tool_checks(
        st, "run_command", {"cmd": f"cat > {other}/x.py <<'EOF'\nx\nEOF"},
        str(ws), None))
    assert events[0]["type"] == "approval"
    assert events[0]["grant_roots"] == [os.path.realpath(other)]
    assert ret == "return"


def test_an_unattended_shell_write_is_left_alone(monkeypatch, tmp_path):
    """Unattended runs are fenced by their worktree and scope allowlist; the
    shell reading is a chat-only prompt, so it never blocks a ticket's build."""
    import types

    from aiforge_core.runtime.chat_agent import _loop
    monkeypatch.setenv("AIFORGE_CHAT_WORKSPACE_JAIL", "1")
    ws = tmp_path / "wt"
    ws.mkdir()
    st = types.SimpleNamespace(convo=[], user_roots=[])
    events, ret = _drive(_loop._pre_tool_checks(
        st, "run_command", {"cmd": "mkdir -p /srv/cache/x"}, str(ws), None))
    assert events == []
    assert ret is None
