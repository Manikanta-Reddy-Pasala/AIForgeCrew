"""'no test dependencies' is not 'no tests'."""
from aiforge_core.runtime.chat_agent._turn._test_guard import asks_for_tests


def test_test_dependencies_do_not_cancel_a_test_request():
    assert asks_for_tests("Detect it. The module has no test dependencies yet. "
                          "Fix it in PosServerBackend with a unit test.")
    assert asks_for_tests("Fix it with a unit test; there is no test framework set up.")


def test_a_real_no_tests_still_cancels():
    assert not asks_for_tests("Fix the bug with a small change, no tests please.")
    assert not asks_for_tests("Write the parser; don't write tests.")
