"""CREATE … TRIGGER Command-fallback hint fires once."""
from types import SimpleNamespace

from aiforge_core.runtime.chat_agent._turn._parse_consume_hint import after_test


def test_fires_once_on_command_trigger():
    st = SimpleNamespace()
    result = {
        "ok": False,
        "stdout": (
            "AssertionError: Command(this=CREATE, expression=CONSTRAINT TRIGGER "
            "check_integrity AFTER INSERT ON users ...) is not an instance of "
            "<class 'sqlglot.expressions.Create'>"
        ),
    }
    note = after_test(st, "run_command", {"cmd": "pytest -q"}, result)
    assert "do not call" in note and "_match(TokenType.TRIGGER)" in note
    assert after_test(st, "run_command", {"cmd": "pytest -q"}, result) == ""


def test_skips_unrelated_failure():
    st = SimpleNamespace()
    result = {"ok": False, "stdout": "AssertionError: 1 != 2"}
    assert after_test(st, "run_command", {"cmd": "pytest -q"}, result) == ""
