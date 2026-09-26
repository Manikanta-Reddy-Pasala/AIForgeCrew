"""The team verdict says "all tests pass" only when tests actually ran.

Live bug: a run whose tests never executed answered "**Pipeline complete** —
2/2 subtasks built + merged. ✅ **Built — all tests pass.**"
"""
from __future__ import annotations

from aiforge_core.runtime.parallel_subtasks import _stream as S
from aiforge_core.runtime.parallel_subtasks._test_evidence import (
    executed_tests,
    failed_tests,
    go_packages_passed,
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
    # go test without -v prints packages, not tests: the count is unknown
    assert executed_tests("ok  \texample.com/m\t0.01s\n") is None
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


# ─── per-runner counts from real-looking output ───────────────────────────

_CARGO = """running 5 tests
test tests::a ... ok
test result: ok. 4 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s

     Running unittests src/bin.rs
running 2 tests
test result: ok. 2 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s

   Doc-tests money

running 0 tests
test result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s
"""

_SUREFIRE = """[INFO] Running com.x.MoneyTest
[INFO] Tests run: 3, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 0.05 s -- in com.x.MoneyTest
[INFO] Running com.x.FmtTest
[ERROR] Tests run: 4, Failures: 1, Errors: 1, Skipped: 0, Time elapsed: 0.1 s <<< FAILURE! -- in com.x.FmtTest
[INFO]
[INFO] Results:
[INFO]
[ERROR] Tests run: 7, Failures: 1, Errors: 1, Skipped: 0
"""

_GO_V = """=== RUN   TestFmt
--- PASS: TestFmt (0.00s)
=== RUN   TestTrim
    --- PASS: TestTrim/zero (0.00s)
--- FAIL: TestTrim (0.00s)
FAIL
FAIL\texample.com/money\t0.004s
ok  \texample.com/other\t0.002s
"""

_DOTNET = ("Passed!  - Failed:     0, Passed:     5, Skipped:     0, "
           "Total:     5, Duration: 12 ms - Money.Tests.dll (net8.0)")
_DOTNET_OLD = "Total tests: 6\n     Passed: 5\n     Failed: 1\n"


def test_cargo_sums_every_binary_not_the_trailing_doc_tests():
    assert executed_tests(_CARGO) == 7
    assert failed_tests(_CARGO) == 1


def test_surefire_takes_the_total_not_per_class_plus_total():
    assert executed_tests(_SUREFIRE) == 7
    assert failed_tests(_SUREFIRE) == 2


def test_go_counts_top_level_test_cases_not_packages():
    assert executed_tests(_GO_V) == 2
    assert failed_tests(_GO_V) == 1
    assert go_packages_passed("ok  \texample.com/m\t0.01s\n"
                              "ok  \texample.com/n\t[no test files]\n") == 1


def test_dotnet_and_gradle_formats():
    assert executed_tests(_DOTNET) == 5 and failed_tests(_DOTNET) == 0
    assert executed_tests(_DOTNET_OLD) == 6 and failed_tests(_DOTNET_OLD) == 1
    assert executed_tests("6 tests completed, 2 failed") == 6
    assert failed_tests("6 tests completed, 2 failed") == 2


def test_a_go_run_without_v_is_green_but_uncounted(tmp_path):
    out = S._build_verdict(True, str(tmp_path), "ok  \texample.com/m\t0.01s\n")
    assert "go test" in out and "all" not in out
