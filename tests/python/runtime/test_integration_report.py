"""The build+test report the pipeline hands back, and the managed test venv.

The verdict has three states and they mean different things: True (green),
False (the code failed), and None (the checks could not RUN here — no
toolchain, no project), which is why an absent toolchain must never be
reported as a failure. Manual steps come from the stack that was actually
tested, not a second detection pass — that race is what printed "npm install"
for a pytest project.

The managed venv exists because a bare tree has no marker to install from. Two
lessons are pinned: pytest presence is checked by IMPORT (a prior round can
leave a venv whose install aborted), and third-party deps install ONE AT A
TIME so a single bad name cannot strand pytest and blind the gate.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from aiforge_core.runtime import integration_report as ir


class _P:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ─── language detection ────────────────────────────────────────────────


@pytest.mark.parametrize("marker,lang", [
    ("pom.xml", "java-maven"),
    ("build.gradle", "java-gradle"),
    ("go.mod", "go"),
    ("Cargo.toml", "rust"),
    ("pyproject.toml", "python"),
    ("package.json", "node"),
    ("composer.json", "php"),
    ("Gemfile", "ruby"),
    ("CMakeLists.txt", "c/c++"),
])
def test_a_marker_file_is_authoritative(tmp_path, marker, lang):
    (tmp_path / marker).write_text("")
    assert ir._detect_lang(str(tmp_path)) == lang


def test_a_maven_marker_beats_a_package_json(tmp_path):
    (tmp_path / "pom.xml").write_text("")
    (tmp_path / "package.json").write_text("{}")
    assert ir._detect_lang(str(tmp_path)) == "java-maven"


def test_extensions_are_the_fallback(tmp_path):
    (tmp_path / "app.go").write_text("")
    assert ir._detect_lang(str(tmp_path)) == "go"


def test_a_stray_js_cannot_shadow_a_python_project(tmp_path):
    """This race is what printed "npm install" for a pytest project."""
    (tmp_path / "app.py").write_text("")
    (tmp_path / "bundle.js").write_text("")
    assert ir._detect_lang(str(tmp_path)) == "python"


def test_vendor_dirs_do_not_decide_the_language(tmp_path):
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules/dep.js").write_text("")
    assert ir._detect_lang(str(tmp_path)) is None


def test_an_empty_tree_has_no_language(tmp_path):
    assert ir._detect_lang(str(tmp_path)) is None


# ─── manual steps ──────────────────────────────────────────────────────


def test_manual_steps_are_numbered_for_the_language(tmp_path):
    md = ir._manual_steps_md(str(tmp_path), "go")
    assert "build & test it yourself (go)" in md
    assert "1. `go build ./...`" in md
    assert "2. `go test ./...`" in md


def test_the_tested_stack_wins_over_re_detection(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    assert "(python)" in ir._manual_steps_md(str(tmp_path), "python")


def test_an_unrecognised_project_says_so(tmp_path):
    assert "No recognised project" in ir._manual_steps_md(str(tmp_path))


def test_kotlin_steps_come_from_the_language_registry(tmp_path):
    assert ir._manual_steps_md(str(tmp_path), "kotlin").count("#") >= 2


# ─── absent toolchain ──────────────────────────────────────────────────


@pytest.mark.parametrize("err", [
    "mvn: command not found",
    "go: no such file or directory",
    "'cargo' is not recognized as an internal or external command",
    "npm is not installed",
    "Unable to locate package",
])
def test_a_missing_toolchain_is_recognised(err):
    assert ir._absent(err) is True


@pytest.mark.parametrize("err", ["2 tests failed", "compile error: undefined x", ""])
def test_a_real_failure_is_not_a_missing_toolchain(err):
    assert ir._absent(err) is False


# ─── third-party imports ───────────────────────────────────────────────


def test_stdlib_names_come_from_the_interpreter():
    names = ir._stdlib_names()
    assert {"os", "sqlite3", "secrets", "hashlib"} <= names


def test_only_third_party_imports_are_returned(tmp_path):
    (tmp_path / "app.py").write_text(
        "import os\nimport pygame\nfrom numpy import array\nimport helpers\n")
    (tmp_path / "helpers.py").write_text("")
    assert ir._third_party_imports(str(tmp_path)) == ["numpy", "pygame"]


def test_a_local_package_directory_is_not_third_party(tmp_path):
    (tmp_path / "mypkg").mkdir()
    (tmp_path / "app.py").write_text("import mypkg\n")
    assert ir._third_party_imports(str(tmp_path)) == []


def test_vendor_dirs_are_not_scanned_for_imports(tmp_path):
    (tmp_path / ".aiforge-venv").mkdir()
    (tmp_path / ".aiforge-venv/x.py").write_text("import onlyinvendor\n")
    assert ir._third_party_imports(str(tmp_path)) == []


def test_an_unreadable_file_does_not_stop_the_scan(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("import pygame\n")
    (tmp_path / "b.py").write_text("import numpy\n")
    real_open = open

    def _fussy(path, *a, **kw):
        if str(path).endswith("a.py"):
            raise PermissionError("locked")
        return real_open(path, *a, **kw)
    monkeypatch.setattr("builtins.open", _fussy)
    assert ir._third_party_imports(str(tmp_path)) == ["numpy"]


def test_a_missing_tree_has_no_local_names(tmp_path):
    assert ir._local_names(str(tmp_path / "gone")) == set()


# ─── test discovery ────────────────────────────────────────────────────


def test_both_test_naming_conventions_are_found(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_a.py").write_text("")
    (tmp_path / "b_test.py").write_text("")
    assert len(ir._python_test_files(str(tmp_path))) == 2


def test_vendored_dirs_are_skipped(tmp_path):
    (tmp_path / ".aiforge-venv" / "lib").mkdir(parents=True)
    (tmp_path / ".aiforge-venv/lib/test_vendor.py").write_text("")
    assert ir._python_test_files(str(tmp_path)) == []


def test_a_workspace_under_a_dot_aiforge_root_still_discovers_tests(tmp_path):
    """A substring check on the ABSOLUTE path dropped every test when the
    workspace itself lived under ~/.aiforge/chat-workspaces — which silently
    disabled the reconcile's pass/fail gate for every chat-mode run."""
    ws = tmp_path / ".aiforge" / "chat-workspaces" / "run1"
    ws.mkdir(parents=True)
    (ws / "test_a.py").write_text("")
    assert len(ir._python_test_files(str(ws))) == 1


# ─── the managed venv ──────────────────────────────────────────────────


@pytest.fixture
def pip(monkeypatch):
    calls: list = []
    state = {"has_pytest": False}

    def _run(cmd, **kw):
        calls.append(cmd)
        if cmd[1:] == ["-c", "import pytest"]:
            return _P(returncode=0 if state["has_pytest"] else 1)
        return _P()
    monkeypatch.setattr(subprocess, "run", _run)
    return {"calls": calls, "state": state}


def test_an_existing_working_venv_is_reused(tmp_path, pip, monkeypatch):
    pip["state"]["has_pytest"] = True
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    ir._ensure_pytest_venv(str(tmp_path), "/venv", "/venv/bin/python", 60)
    assert len(pip["calls"]) == 1                 # just the import probe


def test_presence_is_checked_by_import_not_by_the_directory(tmp_path, pip, monkeypatch):
    """A prior round can leave a venv whose pip install aborted — the gate was
    then permanently blind."""
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    ir._ensure_pytest_venv(str(tmp_path), "/venv", "/venv/bin/python", 60)
    assert any("pytest" in c for c in pip["calls"][1])   # it installed anyway


def test_the_core_test_deps_install_together(tmp_path, pip, monkeypatch):
    monkeypatch.setattr(os.path, "exists", lambda p: False)
    ir._ensure_pytest_venv(str(tmp_path), "/venv", "/venv/bin/python", 60)
    core = next(c for c in pip["calls"] if "pytest-asyncio" in c)
    assert {"pytest", "pytest-cov", "pytest-mock", "pytest-timeout", "ruff"} <= set(core)


def test_third_party_deps_install_one_at_a_time(tmp_path, pip, monkeypatch):
    """A single unresolvable name must not abort the install and strand
    pytest."""
    (tmp_path / "app.py").write_text("import pygame\nimport numpy\n")
    monkeypatch.setattr(os.path, "exists",
                        lambda p: p.endswith("python"))
    ir._ensure_pytest_venv(str(tmp_path), "/venv", "/venv/bin/python", 60)
    installs = [c[-1] for c in pip["calls"] if c[:4] ==
                ["/venv/bin/python", "-m", "pip", "-q"] and len(c) == 6]
    assert installs == ["numpy", "pygame"]


def test_a_requirements_file_is_installed_too(tmp_path, pip, monkeypatch):
    (tmp_path / "requirements.txt").write_text("flask\n")
    ir._ensure_pytest_venv(str(tmp_path), "/venv", "/venv/bin/python", 60)
    assert any("-r" in c for c in pip["calls"])


# ─── running pytest ────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [("30", "30"), ("1", "3"), ("junk", "20")])
def test_the_per_test_timeout_is_clamped(monkeypatch, raw, expected):
    """A generated `while True` worker otherwise hangs the WHOLE run and masks
    every other result."""
    monkeypatch.setenv("AIFORGE_PYTEST_TIMEOUT", raw)
    assert ir._pytest_timeout_args() == ["--timeout", expected]


def test_the_default_per_test_timeout(monkeypatch):
    monkeypatch.delenv("AIFORGE_PYTEST_TIMEOUT", raising=False)
    assert ir._pytest_timeout_args() == ["--timeout", "20"]


def test_a_normal_run_is_not_retried(monkeypatch):
    calls: list = []
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or _P(stdout="2 passed"))
    rc, out = ir._run_pytest_capturing("/py", "/cwd", {}, 60)
    assert rc == 0
    assert "2 passed" in out
    assert len(calls) == 1


@pytest.mark.parametrize("first", [
    _P(returncode=4, stdout="usage: pytest [options]"),
    _P(returncode=1, stdout="unrecognized arguments: --cov"),
])
def test_a_broken_config_is_retried_with_addopts_stripped(monkeypatch, first):
    calls: list = []

    def _run(cmd, **kw):
        calls.append(cmd)
        return first if len(calls) == 1 else _P(stdout="3 passed")
    monkeypatch.setattr(subprocess, "run", _run)
    rc, out = ir._run_pytest_capturing("/py", "/cwd", {}, 60)
    assert rc == 0
    assert "3 passed" in out
    assert "addopts=" in calls[1]


# ─── the bare-python path ──────────────────────────────────────────────


def test_a_tree_with_no_tests_has_nothing_to_run(tmp_path):
    assert ir.run_bare_python_tests(str(tmp_path)) is None


@pytest.fixture
def bare(monkeypatch, tmp_path):
    (tmp_path / "test_a.py").write_text("def test_a(): pass\n")
    monkeypatch.setattr(ir, "_ensure_pytest_venv", lambda *a: None)
    monkeypatch.setattr(ir, "_static_lint_python", lambda cwd, py, env: (True, ""))
    state = {"rc": 0, "out": "1 passed"}
    monkeypatch.setattr(ir, "_run_pytest_capturing",
                        lambda py, cwd, env, timeout: (state["rc"], state["out"]))
    monkeypatch.delenv("AIFORGE_LINT_GATE", raising=False)
    return state, tmp_path


def test_a_green_bare_tree_passes(bare):
    state, tmp_path = bare
    assert ir.run_bare_python_tests(str(tmp_path)) == (True, "1 passed")


def test_a_red_bare_tree_reports_its_tail(bare):
    state, tmp_path = bare
    state.update(rc=1, out="x" * 9000)
    ok, out = ir.run_bare_python_tests(str(tmp_path))
    assert ok is False
    assert len(out) == 4000


def test_a_lint_failure_turns_a_green_run_red(bare, monkeypatch):
    state, tmp_path = bare
    monkeypatch.setattr(ir, "_static_lint_python",
                        lambda cwd, py, env: (False, "\nF821 undefined name"))
    ok, out = ir.run_bare_python_tests(str(tmp_path))
    assert ok is False
    assert "F821" in out


def test_the_lint_gate_can_be_turned_off(bare, monkeypatch):
    state, tmp_path = bare
    monkeypatch.setenv("AIFORGE_LINT_GATE", "0")
    monkeypatch.setattr(ir, "_static_lint_python",
                        lambda *a: pytest.fail("linted with the gate off"))
    assert ir.run_bare_python_tests(str(tmp_path))[0] is True


def test_a_crash_in_the_venv_path_reads_as_unrunnable(bare, monkeypatch):
    state, tmp_path = bare
    monkeypatch.setattr(ir, "_ensure_pytest_venv",
                        lambda *a: (_ for _ in ()).throw(OSError("no disk")))
    assert ir.run_bare_python_tests(str(tmp_path)) is None


def test_the_python_linter_reports_real_bug_codes(monkeypatch, tmp_path):
    seen: dict = {}

    def _run(cmd, **kw):
        seen["cmd"] = cmd
        return _P(returncode=1, stdout="a.py:1:1 F821 undefined name 'x'")
    monkeypatch.setattr(subprocess, "run", _run)
    ok, out = ir._static_lint_python(str(tmp_path), "/py", {})
    assert ok is False
    assert "F821" in out
    assert "F821,F822,F811" in seen["cmd"]


def test_a_missing_linter_never_fails_the_run(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("ruff")))
    assert ir._static_lint_python(str(tmp_path), "/py", {}) == (True, "")


# ─── language-native static checks ─────────────────────────────────────


def _dispatch(files_map, present):
    ran: list = []

    def _files(*exts):
        return [f for f in files_map if f.endswith(exts)]

    def _has(exe):
        return exe in present

    def _run(cmd, label, timeout=90):
        ran.append(label)
    ir._dispatch_language_checks("/cwd", _files, _has, _run)
    return ran


def test_typescript_is_typechecked_by_the_compiler(monkeypatch):
    monkeypatch.setattr(os.path, "exists", lambda p: p.endswith("tsconfig.json"))
    assert _dispatch(["app.ts"], {"npx"}) == ["typescript typecheck"]


def test_plain_javascript_is_syntax_checked_per_file(monkeypatch):
    monkeypatch.setattr(os.path, "exists", lambda p: False)
    ran = _dispatch(["a.js", "b.mjs"], {"node"})
    assert len(ran) == 2
    assert all(r.startswith("js syntax") for r in ran)


def test_go_and_rust_get_their_native_linters(monkeypatch):
    monkeypatch.setattr(os.path, "exists", lambda p: False)
    assert _dispatch(["main.go", "lib.rs"], {"go", "cargo"}) == ["go vet",
                                                                "rust clippy"]


def test_a_missing_tool_is_skipped(monkeypatch):
    monkeypatch.setattr(os.path, "exists", lambda p: False)
    assert _dispatch(["main.go"], set()) == []


def test_static_checks_can_be_turned_off(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_LINT_GATE", "0")
    assert ir.run_static_checks(str(tmp_path)) == (True, "")


def test_a_clean_tree_passes_the_static_checks(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_LINT_GATE", raising=False)
    monkeypatch.setattr(ir, "_dispatch_language_checks", lambda *a: None)
    assert ir.run_static_checks(str(tmp_path)) == (True, "")


def test_problems_are_collected_with_their_labels(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_LINT_GATE", raising=False)
    (tmp_path / "main.go").write_text("package main\n")
    import shutil
    monkeypatch.setattr(shutil, "which", lambda exe: "/usr/bin/" + exe)
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: _P(returncode=1, stdout="vet: bad ref"))
    ok, out = ir.run_static_checks(str(tmp_path))
    assert ok is False
    assert "=== go vet ===" in out
    assert "bad ref" in out


def test_a_crashing_checker_is_skipped(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_LINT_GATE", raising=False)
    (tmp_path / "main.go").write_text("package main\n")
    import shutil
    monkeypatch.setattr(shutil, "which", lambda exe: "/usr/bin/" + exe)
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    assert ir.run_static_checks(str(tmp_path)) == (True, "")


# ─── the report ────────────────────────────────────────────────────────


@pytest.fixture
def runner(monkeypatch):
    import aiforge_core.runtime.tools.project_runner as pr
    state = {"stacks": ["python"], "has_tests": True,
             "build": {"ok": True}, "test": {"ok": True}}
    monkeypatch.setattr(pr, "detect", lambda cwd: {"stacks": state["stacks"]})
    monkeypatch.setattr(pr, "_has_tests", lambda cwd, stacks: state["has_tests"])
    monkeypatch.setattr(pr, "project",
                        lambda action, cwd: state["build"] if action == "build"
                        else state["test"])
    return state


def test_a_green_project_reports_both_steps(tmp_path, runner):
    rep = ir.build_and_test_report(str(tmp_path))
    assert rep["ok"] is True
    assert "build/compile:** ✅ passed" in rep["md"]
    assert "tests (end-to-end):** ✅ passed" in rep["md"]
    assert "build & test it yourself (python)" in rep["md"]


def test_failing_tests_make_the_report_red(tmp_path, runner):
    runner["test"] = {"ok": False, "error": "2 failed"}
    rep = ir.build_and_test_report(str(tmp_path))
    assert rep["ok"] is False
    assert "2 failed" in rep["md"]


def test_a_project_with_no_tests_falls_back_to_the_build_result(tmp_path, runner):
    runner["has_tests"] = False
    rep = ir.build_and_test_report(str(tmp_path))
    assert rep["ok"] is True
    assert "_none found_" in rep["md"]


def test_an_absent_build_toolchain_is_not_a_failure(tmp_path, runner):
    """None means "couldn't check here" — reporting False would blame the code
    for a missing compiler."""
    runner["build"] = {"ok": False, "error": "mvn: command not found"}
    rep = ir.build_and_test_report(str(tmp_path))
    assert rep["ok"] is None
    assert "can't auto-build" in rep["md"]


def test_an_absent_test_toolchain_is_not_a_failure(tmp_path, runner):
    runner["test"] = {"ok": False, "error": "pytest: command not found"}
    rep = ir.build_and_test_report(str(tmp_path))
    assert rep["ok"] is None
    assert "Test toolchain isn't installed" in rep["md"]


def test_a_failed_build_still_runs_the_tests(tmp_path, runner):
    runner["build"] = {"ok": False, "error": "compile error"}
    rep = ir.build_and_test_report(str(tmp_path))
    assert "build/compile:** ❌ failed" in rep["md"]
    assert rep["ok"] is True                    # the tests are the strict gate


def test_the_manual_steps_match_the_tested_stack(tmp_path, runner):
    runner["stacks"] = ["maven"]
    assert "(java-maven)" in ir.build_and_test_report(str(tmp_path))["md"]


def test_a_bare_python_tree_still_gets_a_real_verdict(tmp_path, runner, monkeypatch):
    runner["stacks"] = []
    monkeypatch.setattr(ir, "run_bare_python_tests", lambda cwd: (False, "1 failed"))
    rep = ir.build_and_test_report(str(tmp_path))
    assert rep["ok"] is False
    assert "no build marker" in rep["md"]
    assert "1 failed" in rep["md"]


def test_a_bare_tree_with_no_tests_reports_no_markers(tmp_path, runner, monkeypatch):
    runner["stacks"] = []
    monkeypatch.setattr(ir, "run_bare_python_tests", lambda cwd: None)
    rep = ir.build_and_test_report(str(tmp_path))
    assert rep["ok"] is None
    assert "No build markers found" in rep["md"]


def test_a_missing_project_runner_still_returns_manual_steps(tmp_path, monkeypatch):
    import builtins
    real_import = builtins.__import__

    def _no_runner(name, *a, **kw):
        if "project_runner" in name:
            raise ImportError("gone")
        return real_import(name, *a, **kw)
    monkeypatch.setattr(builtins, "__import__", _no_runner)
    rep = ir.build_and_test_report(str(tmp_path))
    assert rep["ok"] is None
    assert "Integration check" in rep["md"]
