"""The native rules: the per-requirement test rule (softening it cost click
and shopkit their scores), examples as tests, the two-attempt limit on side
failures (the finish messages say the same), and tests of code the request
removes go with it (runs kept the old _bashcomplete tests alive: 0/37)."""
from aiforge_core.runtime.chat_agent import _native_prompt as P


def test_the_rules_keep_the_test_rule_and_let_removed_code_tests_go():
    rules = P.native_rules("/r")
    assert "write a test for every requirement" in rules
    assert "an example's exact" in rules and "two focused attempts" in rules
    assert "except tests of code the request removes or replaces" in rules
    assert "Never weaken or delete a test" in rules
    assert "contradicts the request's text" in rules


def test_the_finish_messages_agree_with_the_rule():
    from aiforge_core.runtime.chat_agent._context._verify import _verify_fix_message
    from aiforge_core.runtime.chat_agent._turn import _done_check
    msg = _verify_fix_message("FAILED t.py::x\n1 failed, 2 passed")
    assert "two focused attempts" in msg
    assert "names a side failure still there" in _done_check._SYSTEM
