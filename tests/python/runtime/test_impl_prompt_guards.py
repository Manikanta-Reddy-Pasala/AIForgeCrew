"""Test-first divergence guard + language rules injected into the impl prompt."""
from aiforge_core.runtime.parallel_subtasks._runners import (
    _lang_rules, _required_api_from_tests,
)


def test_required_api_extracts_called_methods_not_asserts():
    t = ("s = Stack()\n"
         "s.push(1)\n"
         "self.assertEqual(s.pop(), 1)\n"
         "assertTrue(s.contains(1))\n"
         "s.size()\n")
    req = _required_api_from_tests(t)
    assert set(["push", "pop", "contains", "size"]).issubset(set(req))
    # test-framework calls are NOT demanded of the impl
    assert not any(a.lower().startswith("assert") for a in req)


def test_required_api_empty_for_no_tests():
    assert _required_api_from_tests("") == []


def test_cpp_gets_template_header_rule():
    for p in ("src/vec.cpp", "include/vec.hpp", "a.cc", "b.cxx"):
        assert "TEMPLATE" in _lang_rules(p)


def test_non_cpp_no_rule():
    for p in ("heap.py", "Stack.java", "lib.rs", "main.go"):
        assert _lang_rules(p) == ""
