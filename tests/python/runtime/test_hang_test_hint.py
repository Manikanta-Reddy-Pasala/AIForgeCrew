"""Hung/killed pytest observation fires the infinite-loop nudge once."""
from types import SimpleNamespace

from aiforge_core.runtime.chat_agent._turn._hang_test_hint import after_tool


def test_fires_once_when_pytest_killed():
    st = SimpleNamespace()
    result = {
        "ok": False,
        "stopped": True,
        "error": "stopped: killed with command_kill",
        "stdout": "",
    }
    note = after_tool(
        st, "run_command",
        {"cmd": "python -m pytest -q tests/"},
        result,
    )
    assert "infinite loop" in note
    assert after_tool(
        st, "run_command",
        {"cmd": "python -m pytest -q tests/"},
        result,
    ) == ""


def test_skips_green_test_run():
    st = SimpleNamespace()
    assert after_tool(
        st, "run_command",
        {"cmd": "pytest -q"},
        {"ok": True, "returncode": 0, "stdout": "8 passed"},
    ) == ""
