"""A test run that 'passed' only because a pipe hid the exit code still gets
the toolchain note (missing test framework)."""
from types import SimpleNamespace

from aiforge_core.runtime.chat_agent._turn import _env_guard, _outcomes


def test_masked_red_run_gets_the_test_dependency_note(monkeypatch, tmp_path):
    (tmp_path / "pom.xml").write_text("<project/>")
    monkeypatch.setattr(_env_guard, "_m2_has", lambda g, a: True)
    st = SimpleNamespace(cwd=str(tmp_path), edits_made=1, goal="Fix it with a unit test.")
    out = ("[ERROR] /r/src/test/java/a/XTest.java:[3,29] package org.junit.jupiter.api does not exist\n"
           "[ERROR] Failed to execute goal ... MojoFailureException")
    _outcomes._note_green_tests(st, "run_command", {"cmd": "mvn test -o -q -Dtest=XTest 2>&1 | tail -50"},
                                {"ok": True, "code": 0, "stdout": out}, str(tmp_path))
    held = " ".join(getattr(st, "held_notes", []) or [])
    assert "junit-jupiter" in held and "<scope>test</scope>" in held
