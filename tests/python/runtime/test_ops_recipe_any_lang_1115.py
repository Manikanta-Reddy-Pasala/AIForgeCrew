"""The run recipe is learned for any toolchain, from the command's FINAL outcome.

Live case (real PosClientBackend job): the agent's working command was
``cd <repo> && JAVA_HOME=/home/ai/jdk24/... PATH=...:$PATH mvn -o -q test
-Dtest=X 2>&1 | tail -40``, run in the background. Nothing was learned:
the learner knew only pytest/unittest/tox and only PYTHONPATH, so the next
session had no recipe and had to rediscover JDK 24.
"""
import pytest

from aiforge_core.runtime import ops_memory, session_ledger

ROOT = "/w/PosClientBackend"


@pytest.fixture(autouse=True)
def _cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_OPS_MEMORY", "1")


def _repo():
    return ops_memory.load()["repos"]["posclientbackend"]


def test_maven_with_jdk_env_is_learned():
    cmd = (f"cd {ROOT} && JAVA_HOME=/home/ai/jdk24/jdk-24.0.2+12 "
           "PATH=/home/ai/jdk24/jdk-24.0.2+12/bin:$PATH mvn -o -q test "
           "-Dtest=InventoryAgingCalculatorTest 2>&1 | tail -40")
    assert ops_memory.learn_repo_command("PosClientBackend", ROOT, cmd)["ok"]
    r = _repo()
    assert r["test_cmd"] == "mvn -o -q test"
    assert r["env"] == ("JAVA_HOME=/home/ai/jdk24/jdk-24.0.2+12 "
                        "PATH=/home/ai/jdk24/jdk-24.0.2+12/bin:$PATH")
    block = ops_memory.context_block("PosClientBackend", "fix a bug")
    assert "JAVA_HOME=/home/ai/jdk24/jdk-24.0.2+12" in block and "mvn -o -q test" in block


@pytest.mark.parametrize("cmd,want", [
    ("npm test -- --testNamePattern=foo", "npm test"),
    ("./gradlew test --tests com.x.FooTest", "./gradlew test"),
    ("go test ./... -run TestFoo", "go test ./..."),
    ("cargo test", "cargo test"),
    ("PYTHONPATH=/w/PosClientBackend/src .venv/bin/python -m pytest tests -k foo",
     ".venv/bin/python -m pytest tests"),
])
def test_other_toolchains(cmd, want):
    ops_memory.learn_repo_command("PosClientBackend", ROOT, cmd)
    assert _repo()["test_cmd"] == want


def test_python_env_stays_relative():
    ops_memory.learn_repo_command(
        "PosClientBackend", ROOT, "PYTHONPATH=/w/PosClientBackend/src pytest -q")
    assert _repo()["env"] == "PYTHONPATH=src"


def test_non_test_command_learns_no_test_cmd():
    ops_memory.learn_repo_command("PosClientBackend", ROOT, "mvn -o -q compile")
    assert "test_cmd" not in (ops_memory.load()["repos"].get("posclientbackend") or {})


def _steps(*steps):
    return {"role": "assistant", "steps": [dict(type="tool", **s) for s in steps]}


def _fold(*msgs):
    seen, order, bg = {}, [], {}
    for m in msgs:
        session_ledger._fold_message_steps(m, seen, order, bg)
    return [seen[k] for k in order]


def test_background_command_takes_its_exit_code():
    start = {"name": "run_command", "args": {"command": "mvn test"},
             "result": {"ok": True, "id": "bg-1", "running": True}}
    fail = {"name": "command_wait", "args": {"id": "bg-1"},
            "result": {"ok": False, "id": "bg-1", "running": False, "code": 1}}
    assert _fold(_steps(start))[0]["outcome"] is None          # not known yet
    assert _fold(_steps(start, fail))[0]["outcome"] is False
    ok = dict(fail, result={"ok": True, "id": "bg-1", "running": False, "code": 0})
    assert _fold(_steps(start), _steps(ok))[0]["outcome"] is True   # across messages


def test_still_running_wait_changes_nothing():
    start = {"name": "run_command", "args": {"command": "mvn test"},
             "result": {"ok": True, "id": "bg-2", "running": True}}
    wait = {"name": "command_wait", "args": {"id": "bg-2"},
            "result": {"ok": True, "id": "bg-2", "running": True}}
    assert _fold(_steps(start, wait))[0]["outcome"] is None


def test_foreground_command_unchanged():
    run = {"name": "run_command", "args": {"command": "ls"}, "result": {"ok": True}}
    assert _fold(_steps(run))[0]["outcome"] is True


def test_regress_never_runs_pytest_in_a_rust_tests_folder(tmp_path):
    from types import SimpleNamespace
    from aiforge_core.runtime.chat_agent._turn import _regress
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "integration.rs").write_text("#[test] fn a() {}\n")
    assert _regress._suite_command(SimpleNamespace(), str(tmp_path)) == ""
    (tmp_path / "tests" / "test_a.py").write_text("def test_a(): pass\n")
    assert "-m pytest" in _regress._suite_command(SimpleNamespace(), str(tmp_path))
