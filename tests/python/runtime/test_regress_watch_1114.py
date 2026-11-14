"""Regression watch: tests passing at the base and failing after edits are named."""
from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest

from aiforge_core.runtime.chat_agent._turn import _regress as R


def test_base_command_strips_narrowing():
    assert R.base_command("cd /r && PYTHONPATH=src python -m pytest tests/test_a.py -k foo -x -q | tail -5") \
        == "cd /r && PYTHONPATH=src python -m pytest -q"
    assert R.base_command("python -m pytest tests/test_x.py::test_one -v") == "python -m pytest"
    assert R.base_command("ls tests") == ""


def test_failing_ids_and_unfinished_runs():
    out = "FAILED tests/t.py::test_a - assert 1\nERROR tests/t.py::test_b\n1 failed, 3 passed in 0.2s"
    assert R._failing(out) == {"tests/t.py::test_a", "tests/t.py::test_b"}
    assert R._failing("tests/t.py ....") is None


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    (r / "tests").mkdir(parents=True)
    (r / "calc.py").write_text("def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n")
    (r / "tests" / "test_calc.py").write_text(
        "import calc\n\ndef test_add():\n    assert calc.add(1, 2) == 3\n\n"
        "def test_sub():\n    assert calc.sub(3, 1) == 2\n")
    g = lambda *a: subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True)
    g("init", "-q"); g("add", "-A"); g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    return r


def test_a_test_broken_by_an_edit_is_named_on_a_narrow_passing_run(repo, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_REGRESS", "1")
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a + b\n")
    st = SimpleNamespace(edits_made=3)
    cmd = f"cd {repo} && PYTHONPATH={repo} {sys.executable} -m pytest tests/test_calc.py -k add"
    note = R.check(st, "run_command", {"cmd": cmd}, {"ok": True, "code": 0}, str(repo))
    assert "test_sub" in note and "passed before your edits" in note
    st.edits_made = 6                                     # same breakage: said once
    assert R.check(st, "run_command", {"cmd": cmd}, {"ok": True, "code": 0}, str(repo)) == ""


def test_nothing_is_said_when_nothing_broke_or_it_is_off(repo, monkeypatch):
    cmd = f"cd {repo} && PYTHONPATH={repo} {sys.executable} -m pytest -k add"
    st = SimpleNamespace(edits_made=3)
    assert R.check(st, "run_command", {"cmd": cmd}, {"ok": True}, str(repo)) == ""      # off
    monkeypatch.setenv("AIFORGE_CHAT_REGRESS", "1")
    assert R.check(st, "run_command", {"cmd": cmd}, {"ok": True}, str(repo)) == ""      # clean
    st2 = SimpleNamespace(edits_made=1)                                                 # too few edits
    assert R.check(st2, "run_command", {"cmd": cmd}, {"ok": True}, str(repo)) == ""
