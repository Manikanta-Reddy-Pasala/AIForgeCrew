"""The finish check gives no verdict when its guessed runner cannot run a
non-Python project at all while the turn's own test command passed on the
same code.

Live case (real PosClientBackend job): the runner ran ``mvn -q test`` on the
system JDK 21 where the repo needs 24 -> "release version 24 not supported",
no tests run (fail count 999). The model was told its tests fail, rewrote
pom.xml to chase it, and "checks still FAIL" was saved into memory.
"""
from __future__ import annotations

from types import SimpleNamespace

from aiforge_core.runtime.chat_agent._turn import _outcomes as O

JDK_ERR = ("[ERROR] Failed to execute goal maven-compiler-plugin: Fatal error "
           "compiling: error: release version 24 not supported")


def _st(tmp_path, **kw):
    return SimpleNamespace(cwd=str(tmp_path), edits_made=1, **kw)


def _patch(monkeypatch, out=JDK_ERR, ok=False):
    monkeypatch.setattr(O, "_run_project_verify", lambda cwd, python=None: (ok, out))
    monkeypatch.setattr(O, "_project_python", lambda st, cwd: None)
    monkeypatch.setattr(O, "_runner_broken_at_base", lambda *a, **k: False)
    monkeypatch.setattr(O, "content_fingerprint", lambda cwd: "fp-now")


def test_env_only_runner_failure_with_own_green_has_no_verdict(tmp_path, monkeypatch):
    _patch(monkeypatch)
    st = _st(tmp_path, own_test_ok_fp="fp-now", last_full_test=None)
    assert O._project_verify(st, str(tmp_path))[0] is None


NOT_RUN = "[ERROR] Failed to execute goal org.example:plugin:1.0:run: plugin crashed"


def test_own_green_on_older_code_keeps_the_failure(tmp_path, monkeypatch):
    _patch(monkeypatch, out=NOT_RUN)
    st = _st(tmp_path, own_test_ok_fp="fp-before-last-edit", last_full_test=None)
    assert O._project_verify(st, str(tmp_path))[0] is False


def test_no_own_green_keeps_the_failure(tmp_path, monkeypatch):
    _patch(monkeypatch, out=NOT_RUN)
    st = _st(tmp_path, last_full_test=None)
    assert O._project_verify(st, str(tmp_path))[0] is False


def test_runner_that_ran_tests_keeps_its_verdict(tmp_path, monkeypatch):
    _patch(monkeypatch, out="Tests run: 12, Failures: 2, Errors: 0\nBUILD FAILURE")
    st = _st(tmp_path, own_test_ok_fp="fp-now", last_full_test=None)
    assert O._project_verify(st, str(tmp_path))[0] is False


def test_note_sets_and_clears_own_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(O, "content_fingerprint", lambda cwd: "fp-now")
    monkeypatch.setattr(O, "_is_test_run", lambda name, args: True)
    st = SimpleNamespace(cwd=str(tmp_path), edits_made=1)
    args = {"command": "JAVA_HOME=/j mvn -o -q test -Dtest=X 2>&1 | tail -40"}
    O._note_green_tests(st, "run_command", args, {"ok": True, "output": ""}, str(tmp_path))
    assert st.own_test_ok_fp == "fp-now"
    O._note_green_tests(st, "run_command", args,
                        {"ok": True, "output": "[ERROR] Tests run: 3, Failures: 1"},
                        str(tmp_path))
    assert st.own_test_ok_fp is None


def test_toolchain_mismatch_has_no_verdict_even_without_own_green(tmp_path, monkeypatch):
    _patch(monkeypatch)
    st = _st(tmp_path, last_full_test=None)            # no own green run at all
    assert O._project_verify(st, str(tmp_path))[0] is None


def test_real_compile_error_keeps_the_failure(tmp_path, monkeypatch):
    _patch(monkeypatch, out="[ERROR] /w/src/main/java/a/Foo.java:[12,5] cannot find symbol\n"
                            "[INFO] BUILD FAILURE")
    st = _st(tmp_path, last_full_test=None)
    assert O._project_verify(st, str(tmp_path))[0] is False


def test_toolchain_mismatch_reruns_the_turns_own_test_command(tmp_path, monkeypatch):
    from aiforge_core.runtime.chat_agent._turn import _done_watch
    _patch(monkeypatch)
    ran = []
    red = ("[ERROR] Tests run: 19, Failures: 1, Errors: 0, Skipped: 0\n"
           "[ERROR]   FooTest.carriesDates:40 expected: <4> but was: <0>\n[INFO] BUILD FAILURE")
    monkeypatch.setattr(_done_watch, "_run", lambda cmd, cwd, timeout=0: ran.append(cmd) or (1, red))
    st = _st(tmp_path, last_full_test=None, last_test_cmd=("run_command", {
        "command": "cd /w && JAVA_HOME=/j mvn -o -q test -Dtest=FooTest 2>&1 | tail -40"}))
    ok, out = O._project_verify(st, str(tmp_path))
    assert ok is False and "carriesDates" in out
    assert ran == ["cd /w && JAVA_HOME=/j mvn -o -q test -Dtest=FooTest"]   # pipe dropped
    monkeypatch.setattr(_done_watch, "_run", lambda cmd, cwd, timeout=0: (
        0, "[INFO] Tests run: 19, Failures: 0, Errors: 0, Skipped: 0\n[INFO] BUILD SUCCESS"))
    assert O._project_verify(st, str(tmp_path))[0] is True


def test_non_python_project_checks_with_the_turns_own_command(tmp_path, monkeypatch):
    from aiforge_core.runtime.chat_agent._turn import _done_watch
    ran = []
    monkeypatch.setattr(O, "_run_project_verify",
                        lambda cwd, python=None: ran.append("guessed") or (False, "x"))
    monkeypatch.setattr(O, "_project_python", lambda st, cwd: None)
    monkeypatch.setattr(O, "_python_project", lambda cwd: False)
    monkeypatch.setattr(_done_watch, "_run", lambda cmd, cwd, timeout=0: (
        0, "[INFO] Tests run: 8, Failures: 0, Errors: 0, Skipped: 0\n[INFO] BUILD SUCCESS"))
    st = _st(tmp_path, last_full_test=None, last_test_cmd=("run_command", {
        "command": "JAVA_HOME=/j mvn -o -q test -Dtest=FooTest"}))
    assert O._project_verify(st, str(tmp_path))[0] is True
    assert ran == []                                  # the whole-suite guess never ran


def test_python_project_keeps_the_full_check(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(O, "_run_project_verify",
                        lambda cwd, python=None: ran.append("guessed") or (True, "5 passed"))
    monkeypatch.setattr(O, "_project_python", lambda st, cwd: None)
    monkeypatch.setattr(O, "_python_project", lambda cwd: True)
    st = _st(tmp_path, last_full_test=None, last_test_cmd=("run_command", {"command": "pytest -q tests/a"}))
    assert O._project_verify(st, str(tmp_path))[0] is True and ran == ["guessed"]


def test_no_own_command_compiles_instead_and_retries_with_a_fitting_jdk(tmp_path, monkeypatch):
    from aiforge_core.runtime.chat_agent._turn import _done_watch, _env_guard
    (tmp_path / "pom.xml").write_text("<project/>")
    monkeypatch.setattr(O, "_run_project_verify", lambda cwd, python=None: (_ for _ in ()).throw(
        AssertionError("whole-suite guess must not run")))
    monkeypatch.setattr(O, "_project_python", lambda st, cwd: None)
    monkeypatch.setattr(O, "_python_project", lambda cwd: False)
    monkeypatch.setattr(_env_guard, "installed_jdks", lambda: [(25, "/j25"), (24, "/j24"), (21, "/j21")])
    ran = []

    def fake(cmd, cwd, timeout=0):
        ran.append(cmd)
        if not cmd.startswith("JAVA_HOME"):
            return 1, JDK_ERR
        return 1, "[ERROR] /w/src/test/java/a/NewTest.java:[3,44] cannot find symbol\n[ERROR] COMPILATION ERROR"
    monkeypatch.setattr(_done_watch, "_run", fake)
    st = _st(tmp_path, last_full_test=None)
    ok, out = O._project_verify(st, str(tmp_path))
    assert ok is False and "do not compile" in out and "NewTest.java" in out
    assert ran == ["mvn -q -o test-compile", "JAVA_HOME=/j24 PATH=/j24/bin:$PATH mvn -q -o test-compile"]


def test_compiling_project_has_no_verdict(tmp_path, monkeypatch):
    from aiforge_core.runtime.chat_agent._turn import _done_watch
    (tmp_path / "Cargo.toml").write_text("[package]")
    monkeypatch.setattr(O, "_project_python", lambda st, cwd: None)
    monkeypatch.setattr(O, "_python_project", lambda cwd: False)
    monkeypatch.setattr(_done_watch, "_run", lambda cmd, cwd, timeout=0: (0, "Finished test profile"))
    assert O._project_verify(_st(tmp_path, last_full_test=None), str(tmp_path))[0] is None
