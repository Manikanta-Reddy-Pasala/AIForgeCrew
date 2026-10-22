# tests/python/runtime/test_recipes_tools.py
import shlex
import pytest
from aiforge_core.runtime import recipes as R
from aiforge_core.runtime import repo_ident
from aiforge_core.runtime.chat_agent import _obs_text
from aiforge_core.runtime.chat_agent._tools import _misc as M
from aiforge_core.runtime.chat_agent._tools import _skills as S


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_RECIPES", "1")


@pytest.fixture
def runner(monkeypatch):
    calls = []
    def run(args, cwd):
        calls.append((dict(args), cwd))
        return {"ok": True, "code": 0, "stdout": "ran", "stderr": ""}
    monkeypatch.setattr("aiforge_core.runtime.chat_agent._shell._t_run_command", run)
    return calls


def test_project_test_action_runs_the_recipe(tmp_path, runner, monkeypatch):
    from aiforge_core.runtime.tools import project_runner as pr
    monkeypatch.setattr(pr, "project", lambda **k: pytest.fail("auto-detect must not run"))
    R.save(str(tmp_path), "test", "./scripts/test.sh", cwd="svc")
    res = M._t_project({"action": "test"}, str(tmp_path))
    assert res["ok"] and res["recipe"] == "test"
    svc = str(tmp_path / "svc")
    assert runner == [({"cmd": f"cd {shlex.quote(svc)} && ./scripts/test.sh"}, svc)]


def test_project_root_recipe_runs_the_bare_command(tmp_path, runner):
    R.save(str(tmp_path), "build", "make")
    M._t_project({"action": "build", "timeout": 60}, str(tmp_path))
    assert runner == [({"cmd": "make", "timeout": 60}, str(tmp_path))]


def test_project_detect_and_unmapped_actions_auto_detect(tmp_path, runner, monkeypatch):
    from aiforge_core.runtime.tools import project_runner as pr
    seen = {}
    monkeypatch.setattr(pr, "project", lambda **k: seen.update(k) or {"ok": True})
    R.save(str(tmp_path), "test", "./t.sh")
    M._t_project({"action": "detect"}, str(tmp_path))
    assert seen["action"] == "detect" and runner == []


@pytest.mark.parametrize("verb", ["deploy", "release"])
def test_project_never_runs_deploy_or_custom_recipes(tmp_path, runner, monkeypatch, verb):
    from aiforge_core.runtime.tools import project_runner as pr
    seen = {}
    monkeypatch.setattr(pr, "project", lambda **k: seen.update(k) or {"ok": True})
    R.save(str(tmp_path), verb, "./ship.sh")
    M._t_project({"action": verb}, str(tmp_path))
    assert seen["action"] == verb and runner == []


def test_recipe_runs_in_the_worktree_not_the_main_repo(tmp_path, runner, monkeypatch):
    main, wt = tmp_path / "main", tmp_path / "wt"
    main.mkdir(); wt.mkdir()
    R.save(str(main), "test", "./scripts/test.sh", cwd="svc")
    # The lookup maps the chat worktree to its main repo; the run must not.
    monkeypatch.setattr(repo_ident, "git_toplevel", lambda cwd: str(main))
    res = M._t_project({"action": "test"}, str(wt))
    assert res["recipe"] == "test"
    svc = str(wt / "svc")
    assert runner == [({"cmd": f"cd {shlex.quote(svc)} && ./scripts/test.sh"}, svc)]


def test_run_tests_tool_runs_the_recipe(tmp_path, runner, monkeypatch):
    from aiforge_core.runtime.tools import test_runner as tr
    monkeypatch.setattr(tr, "run_tests", lambda **k: pytest.fail("auto-detect must not run"))
    R.save(str(tmp_path), "test", "pytest -q tests/")
    res = S._t_run_tests({"mode": "fast"}, str(tmp_path))
    assert res["ok"] and res["recipe"] == "test"
    assert runner[0][0] == {"cmd": "pytest -q tests/"}


def test_run_tests_without_a_recipe_is_unchanged(tmp_path, runner, monkeypatch):
    from aiforge_core.runtime.tools import test_runner as tr
    monkeypatch.setattr(tr, "run_tests", lambda **k: {"ok": True, "via": "auto"})
    assert S._t_run_tests({}, str(tmp_path))["via"] == "auto"


def test_run_tests_with_a_pattern_is_a_targeted_run(tmp_path, runner, monkeypatch):
    from aiforge_core.runtime.tools import test_runner as tr
    seen = {}
    monkeypatch.setattr(tr, "run_tests", lambda **k: seen.update(k) or {"ok": True})
    R.save(str(tmp_path), "test", "pytest -q tests/")
    S._t_run_tests({"pattern": "test_x"}, str(tmp_path))
    assert seen["pattern"] == "test_x" and runner == []


def test_loop_rewrites_a_raw_family_command(tmp_path):
    from aiforge_core.runtime.chat_agent import _loop
    R.save(str(tmp_path), "test", "./t.sh")
    args = {"cmd": "pytest -q"}
    _loop._apply_recipe("run_command", args, str(tmp_path))
    assert args == {"cmd": "./t.sh", "_recipe": "test", "_recipe_was": "pytest -q"}
    other = {"cmd": "ls"}
    _loop._apply_recipe("run_command", other, str(tmp_path))
    assert other == {"cmd": "ls"}
    fr = {"path": "x"}
    _loop._apply_recipe("file_read", fr, str(tmp_path))
    assert fr == {"path": "x"}


def test_loop_rewrite_keeps_the_recipe_folder(tmp_path):
    from aiforge_core.runtime.chat_agent import _loop
    R.save(str(tmp_path), "test", "./scripts/test.sh", cwd="svc")
    args = {"cmd": "pytest -q"}
    _loop._apply_recipe("run_command", args, str(tmp_path))
    svc = str(tmp_path / "svc")
    assert args["cmd"] == f"cd {shlex.quote(svc)} && ./scripts/test.sh"
    assert args["_recipe"] == "test"


def test_loop_rewrite_uses_the_command_key_the_call_used(tmp_path):
    from aiforge_core.runtime.chat_agent import _loop
    R.save(str(tmp_path), "test", "./t.sh")
    args = {"command": "pytest -q"}
    _loop._apply_recipe("bash", args, str(tmp_path))
    assert args == {"command": "./t.sh", "_recipe": "test", "_recipe_was": "pytest -q"}


def test_loop_warn_mode_only_marks(tmp_path, monkeypatch):
    from aiforge_core.runtime.chat_agent import _loop
    monkeypatch.setenv("AIFORGE_CHAT_RECIPE_REPLACE", "0")
    R.save(str(tmp_path), "test", "./t.sh")
    args = {"cmd": "pytest -q"}
    _loop._apply_recipe("run_command", args, str(tmp_path))
    assert args == {"cmd": "pytest -q", "_recipe_warn": "test"}


def test_call_sig_ignores_recipe_marks():
    from aiforge_core.runtime.chat_agent._turn._batch import _call_sig
    a = _call_sig("run_command", {"cmd": "./t.sh", "_recipe": "test", "_recipe_was": "pytest -q"})
    b = _call_sig("run_command", {"cmd": "./t.sh", "_recipe": "test", "_recipe_was": "pytest"})
    assert a == b == _call_sig("run_command", {"cmd": "./t.sh"})
    assert _call_sig("run_command", {"cmd": "ls"}) != a


def test_observation_shows_the_recipe_header():
    out = _obs_text.render_command({"ok": True, "code": 0, "stdout": "ok",
                                    "recipe": "test", "recipe_was": "pytest -q"}, 4000)
    assert out.startswith("[recipe test replaced: pytest -q]\nexit 0")
    out2 = _obs_text.render_command({"ok": True, "code": 0, "stdout": "ok", "recipe": "test"}, 4000)
    assert out2.startswith("[recipe test]\nexit 0")
    warn = _obs_text.render_command({"ok": True, "code": 0, "stdout": "ok", "recipe_warn": "test"}, 4000)
    assert "[this project has a test recipe — use it next time]" in warn
