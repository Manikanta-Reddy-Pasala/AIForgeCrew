"""Stack detection and the canonical build/test/run commands per stack.

This is what turns "run the tests" into the right command for whatever the
agent happens to be looking at, so most of the risk is in detection: a Python
repo carrying an incidental Makefile must not be built as C, a package.json
with the npm-init placeholder script must not count as having tests, and the
tree walks that look for sources or tests are depth-bounded so a large
checkout does not stall the call.

Execution is process-group based on purpose: a `mvn`/`npm` grandchild can hold
the stdout pipe open after the parent exits, so the wait is bounded and the
whole group is killed — on timeout and on Stop.
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from aiforge_core.runtime.tools import project_runner as pr


# ─── detection ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("marker,stack", [
    ("pom.xml", "maven"),
    ("build.gradle", "gradle"),
    ("settings.gradle", "gradle"),
    ("requirements.txt", "python"),
    ("pyproject.toml", "python"),
    ("go.mod", "go"),
    ("Cargo.toml", "rust"),
])
def test_each_manifest_is_detected(tmp_path, marker, stack):
    (tmp_path / marker).write_text("")
    assert pr.detect(str(tmp_path))["stacks"] == [stack]


def test_a_node_stack_carries_its_framework(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"next": "1"}}))
    assert pr.detect(str(tmp_path))["stacks"] == ["node:next"]


@pytest.mark.parametrize("dep,fw", [("vite", "vite"), ("react", "react"),
                                    ("@angular/core", "@angular/core"),
                                    ("vue", "vue")])
def test_frameworks_are_recognised_from_either_dependency_block(tmp_path, dep, fw):
    (tmp_path / "package.json").write_text(
        json.dumps({"devDependencies": {dep: "1"}}))
    assert pr._node_framework(str(tmp_path)) == fw


def test_an_unparseable_package_json_is_plain_node(tmp_path):
    (tmp_path / "package.json").write_text("{not json")
    assert pr._node_framework(str(tmp_path)) == "node"


@pytest.mark.parametrize("lockfile,pm", [("pnpm-lock.yaml", "pnpm"),
                                         ("yarn.lock", "yarn"),
                                         (None, "npm")])
def test_the_package_manager_comes_from_the_lockfile(tmp_path, lockfile, pm):
    if lockfile:
        (tmp_path / lockfile).write_text("")
    assert pr._node_pm(str(tmp_path)) == pm


def test_several_stacks_can_coexist(tmp_path):
    (tmp_path / "pom.xml").write_text("")
    (tmp_path / "package.json").write_text("{}")
    assert pr.detect(str(tmp_path))["stacks"] == ["maven", "node:node"]


def test_a_native_stack_only_claims_an_otherwise_unrecognised_tree(tmp_path):
    """A Python repo may carry an incidental Makefile."""
    (tmp_path / "Makefile").write_text("all:\n\techo hi\n")
    (tmp_path / "pyproject.toml").write_text("")
    assert pr.detect(str(tmp_path))["stacks"] == ["python"]


@pytest.mark.parametrize("files,stack", [
    (["CMakeLists.txt"], "cmake"),
    (["Makefile"], "make"),
    (["GNUmakefile"], "make"),
    (["main.cpp"], "cpp"),
    (["main.c"], "c"),
])
def test_the_native_stack_is_detected_by_build_file_or_sources(tmp_path, files, stack):
    for f in files:
        (tmp_path / f).write_text("")
    assert pr._detect_native_stack(str(tmp_path)) == stack


def test_an_empty_tree_has_no_stack(tmp_path):
    det = pr.detect(str(tmp_path))
    assert det["stacks"] == [] and det["note"] == "no recognised project markers"


def test_the_source_walk_skips_vendor_dirs_and_is_bounded(tmp_path):
    deep = tmp_path / "a" / "b" / "c" / "d" / "e"
    deep.mkdir(parents=True)
    (deep / "main.cpp").write_text("")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.cpp").write_text("")
    assert pr._has_ext(str(tmp_path), pr._CPP_EXTS) is False


# ─── test discovery ────────────────────────────────────────────────────


@pytest.mark.parametrize("fname", [
    "test_store.py", "store_test.go", "store.test.js", "store.test.ts",
    "store.spec.ts", "BookServiceTest.java", "BookServiceTests.java",
    "tests.rs", "test_main.c", "widget_test.cpp",
])
def test_test_files_are_recognised_across_stacks(fname):
    assert pr._looks_like_test_file(fname) is True


@pytest.mark.parametrize("fname", ["store.py", "main.go", "index.js", "App.java"])
def test_ordinary_sources_are_not_tests(fname):
    assert pr._looks_like_test_file(fname) is False


@pytest.mark.parametrize("layout", ["src/test", "tests", "test"])
def test_a_conventional_test_directory_counts(tmp_path, layout):
    (tmp_path / layout).mkdir(parents=True)
    assert pr._has_tests(str(tmp_path), []) is True


def test_a_test_file_anywhere_counts(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "store_test.go").write_text("")
    assert pr._has_tests(str(tmp_path), ["go"]) is True


def test_a_node_project_needs_a_real_test_script(tmp_path):
    """The npm-init placeholder is not a test setup."""
    (tmp_path / "package.json").write_text(json.dumps(
        {"scripts": {"test": 'echo "Error: no test specified" && exit 1'}}))
    assert pr._has_tests(str(tmp_path), ["node:node"]) is False
    (tmp_path / "package.json").write_text(json.dumps(
        {"scripts": {"test": "vitest run"}}))
    assert pr._has_tests(str(tmp_path), ["node:node"]) is True


def test_an_unreadable_package_json_has_no_test_script(tmp_path):
    (tmp_path / "package.json").write_text("{not json")
    assert pr._node_has_test_script(str(tmp_path)) is False


def test_a_tree_with_nothing_test_shaped(tmp_path):
    (tmp_path / "main.py").write_text("")
    assert pr._has_tests(str(tmp_path), ["python"]) is False


def test_the_test_walk_is_bounded(tmp_path):
    deep = tmp_path / "a" / "b" / "c" / "d" / "e"
    deep.mkdir(parents=True)
    (deep / "test_deep.py").write_text("")
    assert pr._has_test_files(str(tmp_path)) is False


# ─── per-stack plans ───────────────────────────────────────────────────


def test_the_maven_plan(tmp_path):
    tools, cmds = pr._stack_plan("maven", str(tmp_path))
    assert tools == ["java", "mvn"]
    assert cmds["build"] == ["mvn -q -DskipTests package"]
    assert cmds["test"] == ["mvn -q test"]


def test_the_gradle_wrapper_is_preferred_when_present(tmp_path):
    tools, cmds = pr._stack_plan("gradle", str(tmp_path))
    assert tools == ["java", "gradle"] and cmds["test"] == ["gradle test"]
    (tmp_path / "gradlew").write_text("")
    tools, cmds = pr._stack_plan("gradle", str(tmp_path))
    assert tools == ["java"] and cmds["test"] == ["./gradlew test"]


@pytest.mark.parametrize("lockfile,pm", [(None, "npm"), ("yarn.lock", "yarn"),
                                         ("pnpm-lock.yaml", "pnpm")])
def test_the_node_plan_uses_the_projects_package_manager(tmp_path, lockfile, pm):
    if lockfile:
        (tmp_path / lockfile).write_text("")
    tools, cmds = pr._stack_plan("node:node", str(tmp_path))
    assert tools == ["node", pm]
    assert cmds["install"][0].startswith(pm)
    assert cmds["run"] == [f"{pm} run start"]


@pytest.mark.parametrize("fw", ["next", "vite", "react", "react-scripts", "vue"])
def test_a_dev_server_framework_runs_the_dev_script(tmp_path, fw):
    _tools, cmds = pr._stack_plan(f"node:{fw}", str(tmp_path))
    assert cmds["run"] == ["npm run dev"]


def test_the_python_plan_prefers_requirements_then_editable(tmp_path):
    _t, cmds = pr._stack_plan("python", str(tmp_path))
    assert cmds["install"] == ["pip install -e ."]
    (tmp_path / "requirements.txt").write_text("")
    _t, cmds = pr._stack_plan("python", str(tmp_path))
    assert cmds["install"] == ["pip install -r requirements.txt"]


@pytest.mark.parametrize("entry", ["main.py", "app.py", "manage.py", "run.py"])
def test_the_python_entry_point_is_discovered(tmp_path, entry):
    (tmp_path / entry).write_text("")
    _t, cmds = pr._stack_plan("python", str(tmp_path))
    assert cmds["run"] == [f"python {entry}"]


def test_a_python_tree_with_no_entry_point_defaults_to_main(tmp_path):
    _t, cmds = pr._stack_plan("python", str(tmp_path))
    assert cmds["run"] == ["python main.py"]


@pytest.mark.parametrize("stack,compiler", [("cpp", "g++"), ("c", "gcc")])
def test_bare_native_sources_compile_to_one_binary(stack, compiler):
    tools, cmds = pr._stack_plan(stack, "/cwd")
    assert tools == [compiler]
    assert cmds["test"][0].endswith("&& ./a.out")
    assert "-prune" in cmds["build"][0]          # the build dir is excluded


@pytest.mark.parametrize("stack,test_cmd", [
    ("go", "go test ./..."),
    ("rust", "cargo test"),
    ("cmake", None),
])
def test_the_static_plans(stack, test_cmd):
    _t, cmds = pr._stack_plan(stack, "/cwd")
    if test_cmd:
        assert cmds["test"] == [test_cmd]
    else:
        assert "ctest" in cmds["test"][0]


def test_a_makefile_without_a_test_target_still_gates():
    _t, cmds = pr._stack_plan("make", "/cwd")
    assert cmds["test"][0].endswith("|| make")


def test_an_unknown_stack_has_no_plan():
    assert pr._stack_plan("cobol", "/cwd") is None
    assert pr._plan("cobol", "build", "/cwd") == ([], [])


def test_an_action_a_stack_does_not_support():
    assert pr._plan("cmake", "install", "/cwd")[1] == []


# ─── execution ─────────────────────────────────────────────────────────


@pytest.fixture()
def cancel(monkeypatch):
    from aiforge_core.runtime import chat_cancel
    state = {"sid": None, "cancelled": False, "tracked": []}
    monkeypatch.setattr(chat_cancel, "active", lambda: state["sid"])
    monkeypatch.setattr(chat_cancel, "is_cancelled", lambda sid: state["cancelled"])
    monkeypatch.setattr(chat_cancel, "track_pgid",
                        lambda sid, pgid: state["tracked"].append((sid, pgid)))
    return state


def test_a_command_runs_and_its_output_is_captured(cancel, tmp_path):
    r = pr._exec("echo hello", str(tmp_path), 30)
    assert r["ok"] is True and r["code"] == 0 and "hello" in r["output"]


def test_a_failing_command_reports_its_code(cancel, tmp_path):
    r = pr._exec("exit 3", str(tmp_path), 30)
    assert r["ok"] is False and r["code"] == 3


def test_output_is_tail_capped(cancel, tmp_path):
    r = pr._exec("python3 -c \"print('x' * 20000)\"", str(tmp_path), 30)
    assert len(r["output"]) == pr._CAP


def test_a_command_that_cannot_start_is_an_error(cancel, monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no shell")))
    assert pr._exec("x", str(tmp_path), 30) == {"cmd": "x", "ok": False,
                                                "error": "no shell"}


def test_a_run_is_tracked_against_the_chat_session(cancel, tmp_path):
    cancel["sid"] = 7
    pr._exec("true", str(tmp_path), 30)
    assert cancel["tracked"] and cancel["tracked"][0][0] == 7


def test_stop_kills_the_whole_process_group(cancel, monkeypatch, tmp_path):
    cancel["sid"] = 7
    cancel["cancelled"] = True
    killed: list = []
    monkeypatch.setattr(pr, "_kill", lambda proc: killed.append(proc))
    r = pr._exec("sleep 5", str(tmp_path), 30)
    assert r == {"cmd": "sleep 5", "ok": False, "stopped": True}
    assert killed


def test_a_timeout_kills_the_group_and_says_so(cancel, monkeypatch, tmp_path):
    killed: list = []
    monkeypatch.setattr(pr, "_kill", lambda proc: killed.append(proc))
    r = pr._exec("sleep 5", str(tmp_path), 0)
    assert r["ok"] is False and "timeout after 0s" in r["error"]
    assert killed


def test_a_grandchild_holding_the_pipe_cannot_hang_the_call(cancel, monkeypatch,
                                                            tmp_path):
    """mvn/gradle/npm spawn daemons that keep stdout open past the deadline."""
    class _Proc:
        pid = 1234
        returncode = 0
        calls = 0

        def poll(self):
            return 0

        def communicate(self, timeout=None):
            _Proc.calls += 1
            if _Proc.calls == 1:
                raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)
            return ("late output", "")
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _Proc())
    killed: list = []
    monkeypatch.setattr(pr, "_kill", lambda proc: killed.append(proc))
    r = pr._exec("mvn test", str(tmp_path), 30)
    assert killed and r["output"] == "late output"


def test_a_second_communicate_failure_still_returns(cancel, monkeypatch, tmp_path):
    class _Proc:
        pid = 1234
        returncode = 1

        def poll(self):
            return 1

        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _Proc())
    monkeypatch.setattr(pr, "_kill", lambda proc: None)
    assert pr._exec("mvn test", str(tmp_path), 30)["output"] == ""


def test_the_killer_escalates_and_never_raises(monkeypatch):
    signals: list = []

    class _Proc:
        pid = 1234
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    from aiforge_core.runtime import proc_signals as _ps
    monkeypatch.setattr(_ps.os, "killpg", lambda pgid, sig: signals.append(sig))
    pr._kill(_Proc())
    assert len(signals) == 2                      # SIGTERM then SIGKILL


def test_killing_a_gone_process_is_not_fatal(monkeypatch):
    monkeypatch.setattr(os, "getpgid",
                        lambda pid: (_ for _ in ()).throw(ProcessLookupError()))
    pr._kill(type("P", (), {"pid": 1})())


# ─── the entry point ───────────────────────────────────────────────────


@pytest.fixture()
def runner(monkeypatch):
    from aiforge_core.runtime.tools import ensure_runtime as er
    state = {"exec": [], "provision": {"ok": True}, "results": {}}
    monkeypatch.setattr(er, "ensure_runtime", lambda tools: state["provision"])

    def _exec(cmd, cwd, timeout):
        state["exec"].append(cmd)
        return state["results"].get(cmd, {"cmd": cmd, "ok": True, "code": 0})
    monkeypatch.setattr(pr, "_exec", _exec)
    return state


def test_detect_is_the_default_action(tmp_path, runner):
    (tmp_path / "go.mod").write_text("")
    out = pr.project(cwd=str(tmp_path))
    assert out["stacks"] == ["go"] and runner["exec"] == []


def test_a_tree_with_no_project_cannot_be_built(tmp_path, runner):
    out = pr.project("build", str(tmp_path))
    assert out["ok"] is False and "no recognised project" in out["error"]


def test_the_stacks_canonical_command_runs(tmp_path, runner):
    (tmp_path / "go.mod").write_text("")
    out = pr.project("test", str(tmp_path))
    assert out["ok"] is True and runner["exec"] == ["go test ./..."]
    assert out["results"][0]["stack"] == "go"


def test_a_failed_toolchain_install_fails_that_stack(tmp_path, runner):
    (tmp_path / "go.mod").write_text("")
    runner["provision"] = {"ok": False, "error": "no network"}
    out = pr.project("build", str(tmp_path))
    assert out["ok"] is False
    assert out["results"][0]["error"] == "toolchain install failed"
    assert runner["exec"] == []


def test_a_failing_command_stops_that_stacks_sequence(tmp_path, runner):
    (tmp_path / "CMakeLists.txt").write_text("")
    cmds = pr._stack_plan("cmake", str(tmp_path))[1]["test"]
    runner["results"][cmds[0]] = {"cmd": cmds[0], "ok": False, "code": 1}
    out = pr.project("test", str(tmp_path))
    assert out["ok"] is False and len(runner["exec"]) == 1


def test_every_detected_stack_is_run(tmp_path, runner):
    (tmp_path / "go.mod").write_text("")
    (tmp_path / "Cargo.toml").write_text("")
    pr.project("build", str(tmp_path))
    assert runner["exec"] == ["go build ./...", "cargo build"]


def test_a_stack_with_no_command_for_the_action_is_skipped(tmp_path, runner):
    (tmp_path / "CMakeLists.txt").write_text("")
    out = pr.project("install", str(tmp_path))
    assert out["ok"] is True and runner["exec"] == []


def test_the_path_is_expanded(tmp_path, runner, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "go.mod").write_text("")
    assert pr.project("detect", "~")["cwd"] == str(tmp_path)
