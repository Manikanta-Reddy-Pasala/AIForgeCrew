"""A saved workflow's script runs as one step."""
import pytest

from aiforge_core.runtime import workflow_run, workflows
from aiforge_core.runtime.tools import command_risk, tool_policy


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_WORKFLOWS_DIR", str(tmp_path / "wf"))
    monkeypatch.setattr(workflows, "_mirror_to_memory", lambda *a, **k: None)

    def save(name, scripts, body="1. build\n2. deploy\n3. check the version"):
        out = workflows.write_workflow(name, "how we do it", body, scope="global",
                                       scripts=scripts)
        assert out["ok"], out
        return out
    return save


def _runner(result=None):
    calls = []

    def run(args, cwd):
        calls.append((args, cwd))
        return dict(result or {"ok": True, "code": 0, "stdout": "deployed v7"})
    run.calls = calls
    return run


def test_one_call_runs_the_script_with_quoted_args(lib, tmp_path):
    lib("Deploy QA", [{"name": "run.sh", "content": "#!/bin/bash\necho ok\n"}])
    runner = _runner()
    res = workflow_run.run("deploy qa", str(tmp_path), runner,
                           args=["v1.2", "two words; rm -rf /"])
    assert res["ok"] and res["workflow"] == "Deploy QA" and res["script"] == "run.sh"
    cmd = runner.calls[0][0]["cmd"]
    assert cmd.startswith("bash ") and cmd.endswith("run.sh v1.2 'two words; rm -rf /'")
    assert "steps" not in res
    assert "finished: every step of it ran" in res["stdout"]
    assert res["stdout"].startswith("deployed v7")


def test_a_failed_script_hands_back_the_written_steps(lib, tmp_path):
    lib("Deploy QA", [{"name": "run.sh", "content": "#!/bin/bash\necho ok\n"}])
    res = workflow_run.run("Deploy QA", str(tmp_path), _runner(
        {"code": 3, "stderr": "rollout timed out"}))
    assert res["ok"] is False and "3. check the version" in res["steps"]
    assert "do not re-run the whole script" in res["hint"]


def test_a_workflow_without_a_script_gives_its_steps_and_says_how_to_script_it(
        lib, tmp_path):
    lib("Release notes", None)
    runner = _runner()
    res = workflow_run.run("Release notes", str(tmp_path), runner)
    assert res["no_script"] and "1. build" in res["steps"] and not runner.calls
    assert "learn_workflow" in res["hint"]


def test_an_unknown_name_runs_nothing(lib, tmp_path):
    runner = _runner()
    res = workflow_run.run("nope", str(tmp_path), runner)
    assert res["ok"] is False and "no workflow named" in res["error"]
    assert not runner.calls


def test_several_scripts_need_a_run_script_or_a_name():
    paths = ["/w/scripts/a.sh", "/w/scripts/b.py"]
    assert workflow_run.pick_script(paths)[0] is None
    assert workflow_run.pick_script(paths, "b.py")[0] == "/w/scripts/b.py"
    assert workflow_run.pick_script(paths, "../x.sh")[0] is None
    assert workflow_run.pick_script(paths + ["/w/scripts/run.sh"])[0] == "/w/scripts/run.sh"
    assert workflow_run.pick_script(["/w/scripts/only.py"])[0] == "/w/scripts/only.py"


def test_timeout_and_background_are_passed_to_the_command(lib, tmp_path):
    lib("Deploy QA", [{"name": "run.sh", "content": "#!/bin/bash\necho ok\n"}])
    runner = _runner()
    workflow_run.run("Deploy QA", str(tmp_path), runner, timeout=90, background=True)
    assert runner.calls[0][0]["timeout"] == 90 and runner.calls[0][0]["background"]


def test_the_gate_judges_the_script_by_its_lines(lib, tmp_path):
    lib("Wipe", [{"name": "run.sh", "test": "skip",
                  "content": "#!/bin/bash\n# tidy\necho start\nsudo rm -rf /var/lib/x\n"}])
    lib("Check", [{"name": "run.sh", "content": "#!/bin/bash\necho fine\n"}])
    lines = workflow_run.script_lines({"name": "Wipe"}, str(tmp_path))
    assert "sudo rm -rf /var/lib/x" in lines and "# tidy" not in lines
    risky = tool_policy.decide("workflow_run", {"name": "Wipe"})
    assert risky["policy"] != tool_policy.ALLOW
    assert risky["risk"] in (command_risk.DANGEROUS, command_risk.CAUTION)
    assert tool_policy.decide("workflow_run", {"name": "Check"})["policy"] == tool_policy.ALLOW
    assert workflow_run.script_lines({"name": "nope"}, str(tmp_path)) == []


def test_the_tool_is_registered_and_runs_through_the_command_tool(lib, tmp_path,
                                                                  monkeypatch):
    from aiforge_core.runtime.chat_agent import _registry, _shell
    from aiforge_core.runtime.chat_agent._tools import _skills
    lib("Deploy QA", [{"name": "run.sh", "content": "#!/bin/bash\necho ok\n"}])
    seen = []
    monkeypatch.setattr(_shell, "_t_run_command",
                        lambda args, cwd: seen.append(args) or {"ok": True, "code": 0})
    tool = next(v for v in vars(_registry).values()
                if isinstance(v, dict) and "workflow_run" in v and "run_command" in v)
    assert tool["workflow_run"] is _skills._t_workflow_run
    res = _skills._t_workflow_run({"name": "Deploy QA", "args": ["x"]}, str(tmp_path))
    assert res["ok"] and seen and seen[0]["cmd"].endswith("run.sh x")
    assert _skills._t_workflow_run({}, str(tmp_path))["ok"] is False


def test_the_prompt_block_points_at_workflow_run(lib, tmp_path):
    lib("Deploy QA", [{"name": "run.sh", "content": "#!/bin/bash\necho ok\n"}])
    block = workflows.auto_context("deploy qa how we do it", str(tmp_path))
    assert 'workflow_run {"name": "Deploy QA"}' in block


def test_a_runbook_that_ran_is_work_done_for_the_zero_edit_check(monkeypatch):
    from types import SimpleNamespace

    from aiforge_core.runtime import action_log
    from aiforge_core.runtime.chat_agent._guards.zero_edit import ZeroEditGuard
    ok = {"type": "tool", "name": "workflow_run", "result": {"ok": True, "code": 0}}
    bad = {"type": "tool", "name": "workflow_run", "result": {"ok": False, "code": 7}}
    assert workflow_run.succeeded_in([bad, ok]) and not workflow_run.succeeded_in([bad])
    assert not workflow_run.succeeded_in(
        [{"name": "workflow_run", "result": {"ok": True, "running": True}}])
    st = SimpleNamespace(session_id=5, head0=None)
    guard = ZeroEditGuard("", False, "", False, [], "")
    monkeypatch.setattr(action_log, "live_steps", lambda _sid: [ok])
    assert guard.evidence(st) is True
