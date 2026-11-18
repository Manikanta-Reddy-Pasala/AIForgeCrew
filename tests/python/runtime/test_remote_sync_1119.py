"""Testing on another machine: the agent is told when its work is not pushed."""
import os
import subprocess

from aiforge_core.runtime import remote_sync as rs

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def _g(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, env=ENV, check=True)


def _repo(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    bare = tmp_path / "origin.git"
    _g(tmp_path, "init", "-q", "--bare", str(bare))
    r = tmp_path / "work"
    r.mkdir()
    _g(r, "init", "-q", "-b", "main")
    (r / "app.py").write_text("x = 1\n")
    _g(r, "add", "-A"); _g(r, "commit", "-qm", "init")
    _g(r, "remote", "add", "origin", str(bare)); _g(r, "push", "-q", "-u", "origin", "main")
    _g(r, "checkout", "-q", "-b", "aiforge/chat-7-ab12")
    return str(r)


SSH = "ssh tester@192.168.70.50 'cd ~/app && git pull && pytest -q'"


def test_unpushed_branch_and_edits_are_named_with_the_fix(tmp_path, monkeypatch):
    r = _repo(tmp_path, monkeypatch)
    with open(os.path.join(r, "app.py"), "w") as fh:
        fh.write("x = 2\n")
    line = rs.note(SSH, "Already up to date.\n1 failed", r)
    assert "aiforge/chat-7-ab12" in line and "never pushed" in line
    assert "not committed" in line and "git push -u origin aiforge/chat-7-ab12" in line
    assert "git fetch origin aiforge/chat-7-ab12 && git checkout aiforge/chat-7-ab12" in line


def test_pushed_work_gets_no_note_unless_the_host_lacks_the_branch(tmp_path, monkeypatch):
    r = _repo(tmp_path, monkeypatch)
    _g(r, "push", "-q", "-u", "origin", "aiforge/chat-7-ab12")
    assert rs.note(SSH, "5 passed", r) is None
    out = "error: pathspec 'aiforge/chat-7-ab12' did not match any file(s) known to git"
    assert "host does not have branch" in rs.note("ssh vm 'cd app && git checkout aiforge/chat-7-ab12'", out, r)


def test_local_commands_and_the_off_switch(tmp_path, monkeypatch):
    r = _repo(tmp_path, monkeypatch)
    assert rs.note("git pull && pytest -q", "", r) is None          # not another host
    assert rs.note("ssh vm 'uptime'", "", r) is None                 # no git or build there
    monkeypatch.setenv("AIFORGE_CHAT_REMOTE_SYNC_NOTE", "0")
    assert rs.note(SSH, "", r) is None


def test_the_note_reaches_the_model_text():
    from aiforge_core.runtime.chat_agent._obs_text import render_command
    out = render_command({"code": 1, "stdout": "x" * 40000, "stderr": "",
                          "remote": "remote check: push it"}, 2000)
    assert "[remote check: push it]" in out[:300]
