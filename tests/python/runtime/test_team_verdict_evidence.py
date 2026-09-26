"""The team verdict says "all tests pass" only when tests actually ran.

Live bug: a run whose tests never executed answered "**Pipeline complete** —
2/2 subtasks built + merged. ✅ **Built — all tests pass.**"
"""
from __future__ import annotations

from aiforge_core.runtime.parallel_subtasks import _stream as S
from aiforge_core.runtime.parallel_subtasks._test_evidence import (
    executed_tests,
    failed_tests,
)


def test_pytest_counts_are_read():
    assert executed_tests("....\n4 passed in 0.12s") == 4
    assert executed_tests("1 failed, 3 passed in 0.2s") == 4
    assert failed_tests("1 failed, 3 passed, 2 errors in 0.2s") == 3


def test_nothing_collected_is_zero():
    assert executed_tests("\nno tests ran in 0.01s\n") == 0
    assert executed_tests("collected 0 items") == 0


def test_other_runners():
    assert executed_tests("Tests run: 5, Failures: 0, Errors: 0") == 5
    assert executed_tests("Ran 3 tests in 0.001s\n\nOK") == 3
    assert executed_tests("ok  \texample.com/m\t0.01s\n") == 1
    assert executed_tests("ok  \texample.com/m\t[no test files]\n") == 0


def test_unreadable_or_empty_output_is_unknown():
    assert executed_tests("") is None
    assert executed_tests(None) is None
    assert executed_tests("BUILD SUCCESS") is None


def test_zero_tests_collected_is_not_success(tmp_path):
    out = S._build_verdict(True, str(tmp_path), "no tests ran in 0.01s")
    assert not out.startswith("✅")
    assert "NO tests were run" in out
    assert "all tests pass" not in out


def test_a_clean_exit_with_no_summary_is_not_success(tmp_path):
    for output in ("", None, "BUILD SUCCESS"):
        out = S._build_verdict(True, str(tmp_path), output)
        assert not out.startswith("✅"), output
        assert "NO tests were run" in out


def test_real_passing_tests_are_success_with_their_count(tmp_path):
    assert S._build_verdict(True, str(tmp_path), "7 passed in 1s") \
        == "✅ **Built — all 7 tests pass.**"


def test_failures_are_counted(tmp_path):
    out = S._build_verdict(False, str(tmp_path), "2 failed, 5 passed in 1s")
    assert out.startswith("⚠️ **Built — 2 tests failed.**")


def test_the_final_message_is_honest_when_no_tests_ran(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "_verify_against_spec", lambda cwd, spec: "")

    def _integration(cwd, res, should_cancel=None, spec_gaps=""):
        res.update(ok=True, output="", rep={"ok": None, "md": ""})
        yield {"type": "thought", "role": "verifier", "text": "building…"}
    monkeypatch.setattr(S, "_reconcile_integration", _integration)
    monkeypatch.setattr(S, "_emit_changes", lambda cwd, sha, **kw: iter(()))
    evs = list(S._finalize(str(tmp_path), [{"slug": "a", "path": "a.py"}],
                           "# SPEC", {"done": 2, "total": 2}, "sha0",
                           lambda: False))
    text = evs[-1]["text"]
    assert "**Pipeline complete** — 2/2" in text
    assert "✅" not in text and "all tests pass" not in text
    assert "NO tests were run" in text
