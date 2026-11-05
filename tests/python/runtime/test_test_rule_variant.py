"""The native rules: OpenCode-style short testing rule (verify with the
repo's tests, report a failure still there after two focused attempts) and
tests of removed code may go — bench runs chased 'green' to the cap and kept
tests the request let go alive (click 0/37)."""
from aiforge_core.runtime.chat_agent import _native_prompt as P


def test_the_rules_bound_test_chasing_and_let_removed_code_tests_go():
    rules = P.native_rules("/r")
    assert "side failure still there after two focused attempts is reported in the" in rules
    assert "except tests of code the request removes or replaces" in rules
    assert "Never weaken or delete a test" in rules and "correct that test" in rules
    assert "use exactly those" in rules and "FINAL:" in rules and "ASK:" in rules
    assert "fix until it is green" not in rules
    assert "write a test for every" in rules and "example's exact input and output" in rules


def test_the_finish_messages_agree_with_the_rule():
    """A verify send-back that says 'do not reply FINAL until green' and a done
    check that judges an honest 'X still fails' UNFINISHED contradict the rule."""
    from aiforge_core.runtime.chat_agent._context._verify import _verify_fix_message
    from aiforge_core.runtime.chat_agent._turn import _done_check
    msg = _verify_fix_message("FAILED t.py::x\n1 failed, 2 passed")
    assert "Do NOT reply FINAL until" not in msg and "two focused attempts" in msg
    assert "names a side failure still there" in _done_check._SYSTEM
    assert "NOT what the request asked to fix" in _done_check._SYSTEM
