# tests/python/runtime/test_recipes_tools.py
import pytest
from aiforge_core.runtime import recipes as R
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
    assert runner == [({"cmd": "./scripts/test.sh"}, str(tmp_path / "svc"))]


def test_project_detect_and_unmapped_actions_auto_detect(tmp_path, runner, monkeypatch):
    from aiforge_core.runtime.tools import project_runner as pr
    seen = {}
    monkeypatch.setattr(pr, "project", lambda **k: seen.update(k) or {"ok": True})
    R.save(str(tmp_path), "test", "./t.sh")
    M._t_project({"action": "detect"}, str(tmp_path))
    assert seen["action"] == "detect" and runner == []


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


def test_loop_warn_mode_only_marks(tmp_path, monkeypatch):
    from aiforge_core.runtime.chat_agent import _loop
    monkeypatch.setenv("AIFORGE_CHAT_RECIPE_REPLACE", "0")
    R.save(str(tmp_path), "test", "./t.sh")
    args = {"cmd": "pytest -q"}
    _loop._apply_recipe("run_command", args, str(tmp_path))
    assert args == {"cmd": "pytest -q", "_recipe_warn": "test"}


def test_observation_shows_the_recipe_header():
    out = _obs_text.render_command({"ok": True, "code": 0, "stdout": "ok",
                                    "recipe": "test", "recipe_was": "pytest -q"}, 4000)
    assert out.startswith("[recipe test replaced: pytest -q]\nexit 0")
    out2 = _obs_text.render_command({"ok": True, "code": 0, "stdout": "ok", "recipe": "test"}, 4000)
    assert out2.startswith("[recipe test]\nexit 0")
    warn = _obs_text.render_command({"ok": True, "code": 0, "stdout": "ok", "recipe_warn": "test"}, 4000)
    assert "[this project has a test recipe — use it next time]" in warn
