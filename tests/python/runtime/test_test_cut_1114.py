"""A pytest run that stops before its summary line is not a pass (_test_cut)."""
from __future__ import annotations

import subprocess
import sys

from aiforge_core.runtime.chat_agent._turn import _test_cut as C

CUT = ("============================= test session starts ==============================\n"
       "platform linux -- Python 3.12.3, pytest-7.4.4\ncollected 35 items\n\n"
       "tests/test_basic.py ")


def _r(out, code=0):
    return {"ok": code == 0, "code": code, "stdout": out, "stderr": ""}


def test_a_cut_run_with_exit_zero_is_marked():
    res = C.mark("run_command", {"cmd": "python -m pytest tests"}, _r(CUT))
    assert res["tests_incomplete"] and res["ok"] is False and "NOT a pass" in res["warning"]


def test_a_quiet_run_cut_after_its_dots_is_marked():
    res = C.mark("run_command", {"cmd": "pytest -q tests"}, _r("........"))
    assert res.get("tests_incomplete")
    ok = _r("........\n8 passed in 0.1s\n")
    assert C.mark("run_command", {"cmd": "pytest -q"}, ok) is ok


def test_finished_runs_are_left_alone():
    for tail in ("5 passed in 0.31s", "1 failed, 4 passed in 0.2s",
                 "=== 2 passed, 1 skipped in 1.02s ===", "no tests ran in 0.01s",
                 "3 passed, 2 warnings in 0.5s"):
        r = _r(CUT + "....\n" + tail + "\n")
        assert C.mark("run_command", {"cmd": "pytest -q"}, r) is r


def test_cases_where_a_missing_summary_proves_nothing():
    cut = _r(CUT)
    assert C.mark("run_command", {"cmd": "pytest tests | head -40"}, cut) is cut
    assert C.mark("run_command", {"cmd": "pytest --collect-only"}, cut) is cut
    assert C.mark("run_command", {"cmd": "ls tests"}, cut) is cut
    failed = _r(CUT, code=1)
    assert C.mark("run_command", {"cmd": "pytest"}, failed) is failed       # already red
    bg = {**cut, "running": True}
    assert C.mark("run_command", {"cmd": "pytest"}, bg) is bg


def test_the_off_switch(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_TEST_CUT", "0")
    cut = _r(CUT)
    assert C.mark("run_command", {"cmd": "pytest"}, cut) is cut


def test_a_real_os_exit_inside_a_test(tmp_path):
    (tmp_path / "test_x.py").write_text(
        "import os\n\ndef test_a():\n    assert True\n\n"
        "def test_b():\n    os._exit(0)\n\ndef test_c():\n    assert True\n")
    p = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", str(tmp_path)],
                       capture_output=True, text=True, cwd=tmp_path)
    assert p.returncode == 0                                   # the trap: it looks green
    res = C.mark("run_command", {"cmd": "python -m pytest ."},
                 {"ok": True, "code": p.returncode, "stdout": p.stdout, "stderr": p.stderr})
    assert res.get("tests_incomplete")
