"""The team_repo_net safety net on the Doer's own shell tools — the sequential
ADK team and the graph-pipeline Doer call run_shell / bash / command_* through
ADK FunctionTools, not the chat loop."""
from __future__ import annotations

import os
import subprocess

import pytest

from aiforge_core.runtime import team_repo_net as net
from aiforge_core.runtime import team_run_life as life
from aiforge_core.runtime import team_workspace as tw
from aiforge_core.runtime.parallel_subtasks import _protected as prot

pytest.importorskip("google.adk")

USER_EDIT = "def fmt(x):\n    return str(x)\n# the user's own edit\n"


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True)


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(k, "t")
    for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(k, "t@t")
    monkeypatch.setitem(life._SWEPT, "done", True)
    yield
    prot._REG.clear()
    tw._RUNS.clear()
    life._EXCL.clear()
    net._PENDING.clear()


@pytest.fixture
def run(tmp_path):
    from aiforge_core.runtime import sandbox
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "money.py").write_text("def fmt(x):\n    return str(x)\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    (repo / "money.py").write_text(USER_EDIT)
    ws = tw.open_run(str(repo), "fix money.py")
    tok = sandbox.set_root_override(ws.cwd)
    yield os.path.realpath(str(repo)), ws
    sandbox.reset_root_override(tok)
    list(tw.close(ws))


def _tool(name):
    from aiforge_core.runtime import doer_tools
    return next(t for t in doer_tools.adk_function_tools()
                if (getattr(t, "name", None) or t.func.__name__) == name)


def _state(repo):
    return (_git(repo, "status", "--porcelain").stdout,
            _git(repo, "rev-parse", "HEAD").stdout,
            open(os.path.join(repo, "money.py")).read())


@pytest.mark.parametrize("tool", ["run_shell", "shell", "run"])
@pytest.mark.parametrize("tmpl", [
    "cd {repo} && echo x > f.txt",
    "git -C {repo} commit -qam agent",
    "env -C {repo} sh -c 'echo x > f.txt'",
])
def test_doer_shell_tools_put_the_repo_back(run, tool, tmpl):
    repo, ws = run
    before = _state(repo)
    result = _tool(tool).func(cmd=tmpl.format(repo=repo))
    if result.get("error") != "changed_users_checkout":
        assert _state(repo) == before
        pytest.skip("the command had no effect on this platform")
    assert _state(repo) == before
    assert ws.cwd in result["hint"] and "put back" in result["note"]
    assert not os.path.exists(os.path.join(repo, "f.txt"))


def test_a_doer_command_in_the_worktree_is_untouched(run):
    repo, ws = run
    result = _tool("run_shell").func(cmd="echo x > f.txt && cat f.txt")
    assert result["ok"] is True and "note" not in result
    assert os.path.exists(os.path.join(ws.cwd, "f.txt"))
    assert _state(repo)[2] == USER_EDIT


def test_every_shell_capable_doer_tool_is_wrapped_and_keeps_its_schema():
    from aiforge_core.runtime import doer_tools
    from aiforge_core.runtime.doer_tools._net_wrap import SHELL_CAPABLE
    from aiforge_core.runtime.doer_tools._threaded import tool_for
    tools = doer_tools.adk_function_tools()
    seen = set()
    for t in tools:
        name = getattr(t, "name", None) or t.func.__name__
        if name in SHELL_CAPABLE:
            seen.add(name)
            assert getattr(t.func, "_team_net", False), name
            assert t._get_declaration().name == name
    assert {"run_shell", "bash", "command_wait", "command_output",
            "command_kill"} <= seen
    from google.adk.tools import FunctionTool
    wrapped = tool_for(doer_tools.run_shell)._get_declaration()
    plain = FunctionTool(func=doer_tools.run_shell)._get_declaration()
    assert wrapped.model_dump() == plain.model_dump()
    assert "cmd" in str(wrapped.model_dump())


def test_outside_a_team_run_the_doer_tool_is_plain(tmp_path):
    from aiforge_core.runtime import sandbox
    tok = sandbox.set_root_override(str(tmp_path))
    try:
        result = _tool("run_shell").func(cmd="echo hi")
    finally:
        sandbox.reset_root_override(tok)
    assert result["ok"] is True and "note" not in result
