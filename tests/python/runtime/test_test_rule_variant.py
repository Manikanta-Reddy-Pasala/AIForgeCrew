"""The native rules: OpenCode-style short testing rule (verify with the
repo's tests, report a failure still there after two focused attempts) and
tests of removed code may go — bench runs chased 'green' to the cap and kept
tests the request let go alive (click 0/37)."""
from aiforge_core.runtime.chat_agent import _native_prompt as P


def test_the_rules_bound_test_chasing_and_let_removed_code_tests_go():
    rules = P.native_rules("/r")
    assert "after two focused attempts is reported in the answer" in rules
    assert "except tests of code the request removes or replaces" in rules
    assert "Never weaken or delete a test" in rules and "correct that test" in rules
    assert "use exactly those" in rules and "FINAL:" in rules and "ASK:" in rules
    assert "fix until it is green" not in rules
