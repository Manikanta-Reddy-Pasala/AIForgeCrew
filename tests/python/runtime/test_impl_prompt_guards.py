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


def test_greenfield_detection(tmp_path):
    import subprocess
    from aiforge_core.runtime.parallel_subtasks._reconcile._sources import _is_greenfield
    def g(a): subprocess.run(["git","-C",str(tmp_path)]+a, capture_output=True)
    g(["init"]); g(["config","user.email","t@t"]); g(["config","user.name","t"])
    (tmp_path/".gitignore").write_text("x"); g(["add","-A"]); g(["commit","-m","workspace baseline"])
    (tmp_path/"Stack.java").write_text("class Stack{}"); g(["add","-A"]); g(["commit","-m","build"])
    assert _is_greenfield(str(tmp_path)) is True     # source added AFTER baseline
