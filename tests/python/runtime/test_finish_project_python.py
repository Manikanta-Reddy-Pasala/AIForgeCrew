"""The finish check runs the project's tests with the project's interpreter
when one is known, instead of building .aiforge-venv and pip-installing
guessed import names (uc-feature: ~42 s of 86, packages `a`, `b`, `base`)."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

from aiforge_core.runtime import _report_pytest as R
from aiforge_core.runtime import integration_report as IR
from aiforge_core.runtime.chat_agent._turn import _outcomes as O


def test_a_usable_project_python_skips_the_managed_venv(monkeypatch, tmp_path):
    (tmp_path / "test_x.py").write_text("def test_a():\n    assert True\n")
    built = []
    monkeypatch.setattr(IR, "_ensure_pytest_venv", lambda *a: built.append(1) or "")
    monkeypatch.setenv("AIFORGE_LINT_GATE", "0")
    ok, out = R.run_bare_python_tests(str(tmp_path), python=sys.executable)
    assert ok is True and "1 passed" in out and not built
    assert not os.path.exists(tmp_path / ".aiforge-venv")


def test_no_usable_python_falls_back_to_the_managed_venv(monkeypatch, tmp_path):
    (tmp_path / "test_x.py").write_text("def test_a():\n    assert True\n")
    built = []
    monkeypatch.setattr(IR, "_ensure_pytest_venv", lambda *a: built.append(1) or "no index")
    assert R.run_bare_python_tests(str(tmp_path), python="/nope/python") == (False, "no index")
    assert built


def test_the_turns_interpreter_is_the_project_python(monkeypatch, tmp_path):
    from aiforge_core.runtime import shell_writes
    monkeypatch.setattr(shell_writes, "command_of", lambda x: x.get("cmd"))
    st = SimpleNamespace(last_test_cmd=(1, {"cmd": f"{sys.executable} -m pytest tests -q"}),
                         last_full_test=None)
    assert O._project_python(st, str(tmp_path)) == sys.executable
    st2 = SimpleNamespace(last_test_cmd=None, last_full_test=None)
    assert O._project_python(st2, str(tmp_path)) is None          # no own command: runner's venv
    st3 = SimpleNamespace(last_test_cmd=(1, {"cmd": "/r/.aiforge-venv/bin/python -m pytest"}),
                          last_full_test=None)
    assert O._project_python(st3, str(tmp_path)) is None
