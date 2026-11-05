"""AIFORGE_CHAT_TEST_RULE=bounded swaps the 'fix until green' testing rule for
one that reports a failure after two focused attempts (A/B switch)."""
from aiforge_core.runtime.chat_agent import _native_prompt as P


def test_the_default_rule_is_unchanged_and_the_variant_swaps_it(monkeypatch):
    monkeypatch.delenv("AIFORGE_CHAT_TEST_RULE", raising=False)
    full = P.native_rules("/r")
    assert P._TESTS_FULL in full and P._TESTS_BOUNDED not in full
    monkeypatch.setenv("AIFORGE_CHAT_TEST_RULE", "bounded")
    bounded = P.native_rules("/r")
    assert P._TESTS_BOUNDED in bounded and P._TESTS_FULL not in bounded


def test_tests_the_request_lets_go_are_deleted_not_kept_alive():
    """click: 'tests for the old _bashcomplete module, which you may delete'
    — runs that kept them alive (deprecation filters, old API patched)
    scored 0/37; runs that left them alone scored 16-20."""
    rules = P.native_rules("/r")
    assert "unless the request itself says those tests may go" in rules
