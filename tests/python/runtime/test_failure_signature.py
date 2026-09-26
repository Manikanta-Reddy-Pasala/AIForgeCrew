# ruff: noqa: E501 — real runner output is kept verbatim
"""failure_signature: the same failure reads the same on every run, whatever
the runner, and different failures read differently."""
from __future__ import annotations

import pytest

from aiforge_core.runtime.failure_signature import (
    failure_of,
    result_text,
    runner_green,
    signature,
    signature_of,
)

PYTEST_A = """\
============================= test session starts ==============================
platform linux -- Python 3.12.1, pytest-8.2.0, pluggy-1.5.0
rootdir: /tmp/pytest-of-mani/pytest-12/test_x0
collected 3 items

tests/test_calc.py .F.                                                   [100%]

=================================== FAILURES ===================================
___________________________________ test_add ___________________________________

    def test_add():
>       assert add(1, 2) == 4
E       assert 3 == 4

tests/test_calc.py:7: AssertionError
=========================== short test summary info ============================
FAILED tests/test_calc.py::test_add - assert 3 == 4
========================= 1 failed, 2 passed in 0.12s ==========================
"""

PYTEST_B = """\
tests/test_calc.py F..                                                   [100%]
=================================== FAILURES ===================================
E       assert 5 == 4
tests/test_calc.py:9: AssertionError
FAILED tests/test_calc.py::test_add - assert 5 == 4
1 failed, 2 passed in 3.07s
"""

MAVEN_TEST = """\
[INFO] Running com.acme.CalcTest
[ERROR] Tests run: 2, Failures: 1, Errors: 0, Skipped: 0, Time elapsed: 0.041 s <<< FAILURE! -- in com.acme.CalcTest
[ERROR] com.acme.CalcTest.testAdd -- Time elapsed: 0.012 s <<< FAILURE!
org.opentest4j.AssertionFailedError: expected: <4> but was: <3>
\tat com.acme.CalcTest.testAdd(CalcTest.java:14)
[INFO]
[ERROR] Failures:
[ERROR]   CalcTest.testAdd:14 expected: <4> but was: <3>
[ERROR] Tests run: 2, Failures: 1, Errors: 0, Skipped: 0
[INFO] BUILD FAILURE
[INFO] Total time:  4.210 s
[INFO] Finished at: 2026-09-26T10:11:12+04:00
"""

MAVEN_COMPILE = """\
[INFO] --- maven-compiler-plugin:3.11.0:compile (default-compile) @ app ---
[ERROR] COMPILATION ERROR :
[ERROR] /home/ci/.aiforge-worktrees/run-8f3a/sub-1/src/main/java/com/acme/Calc.java:[12,17] cannot find symbol
  symbol:   variable totl
[INFO] BUILD FAILURE
"""

GRADLE_TEST = """\
> Task :test

CalcTest > addsTwoNumbers() FAILED
    org.opentest4j.AssertionFailedError at CalcTest.kt:11

3 tests completed, 1 failed
> Task :test FAILED
BUILD FAILED in 6s
"""

JEST = """\
 FAIL  src/sum.test.js
  sum
    ✓ adds zero (2 ms)
    ✕ adds numbers (5 ms)

  ● sum › adds numbers

    expect(received).toBe(expected) // Object.is equality
    Expected: 4
    Received: 3

Tests:       1 failed, 1 passed, 2 total
Time:        0.842 s
"""

TSC = """\
src/app.ts(12,5): error TS2322: Type 'string' is not assignable to type 'number'.
src/app.ts(30,9): error TS2304: Cannot find name 'totl'.
"""

TSC_PRETTY = """\
src/app.ts:14:5 - error TS2322: Type 'string' is not assignable to type 'number'.
src/app.ts:31:2 - error TS2304: Cannot find name 'totl'.
"""

GO_TEST = """\
=== RUN   TestAdd
    calc_test.go:9: got 3, want 4
--- FAIL: TestAdd (0.00s)
FAIL
FAIL\tgithub.com/acme/calc\t0.011s
"""

GO_BUILD = """\
# github.com/acme/calc
./calc.go:12:9: undefined: totl
"""

PY_TRACEBACK = """\
Traceback (most recent call last):
  File "/tmp/tmpab12cd/app/main.py", line 42, in <module>
    run()
  File "/tmp/tmpab12cd/app/main.py", line 17, in run
    total = compute(0x7f3a2b)
KeyError: 'price'
"""

NPM = """\
npm ERR! code ELIFECYCLE
npm ERR! errno 1
npm ERR! app@1.0.0 build: `webpack --mode production`
npm ERR! Exit status 1
npm ERR! A complete log of this run can be found in:
npm ERR!     /home/ci/.npm/_logs/2026-09-26T10_11_12_123Z-debug.log
"""

RUSTC = """\
error[E0425]: cannot find value `totl` in this scope
 --> src/main.rs:3:13
  |
3 |     let x = totl + 1;
"""


def test_empty_output_has_no_signature():
    assert signature("") == ""
    assert signature(None) == ""
    assert failure_of("   \n").count == 0


def test_green_output_has_no_signature():
    assert signature("12 passed in 0.40s") == ""
    assert signature("[INFO] BUILD SUCCESS") == ""


def test_pytest_reruns_of_the_same_failure_match():
    a, b = failure_of(PYTEST_A), failure_of(PYTEST_B)
    assert a.signature == b.signature == "test:tests/test_calc.py::test_add"
    assert a.count == 1
    assert "test_add" in a.headline


def test_pytest_a_different_test_is_a_different_failure():
    other = PYTEST_A.replace("test_add", "test_sub")
    assert signature(other) != signature(PYTEST_A)


def test_pytest_count_is_the_distinct_failures():
    out = ("FAILED tests/a.py::t1 - x\nFAILED tests/a.py::t2 - y\n"
           "ERROR tests/b.py::t3 - fixture\n")
    assert failure_of(out).count == 3


def test_pytest_collection_error():
    out = "_____ ERROR collecting tests/test_a.py _____\nImportError: no module x\n"
    assert "tests/test_a.py" in signature(out)


def test_ansi_colours_do_not_change_the_signature():
    coloured = PYTEST_A.replace("FAILED", "\x1b[31mFAILED\x1b[0m")
    assert signature(coloured) == signature(PYTEST_A)


def test_maven_test_failure_names_the_test_not_the_timing():
    sig = signature(MAVEN_TEST)
    assert "junit:com.acme.CalcTest" in sig
    assert "junit:com.acme.CalcTest.testAdd" in sig
    assert "0.012" not in sig and "Time elapsed" not in sig
    slower = MAVEN_TEST.replace("0.041 s", "1.902 s").replace("0.012 s", "0.9 s")
    assert signature(slower) == sig


def test_maven_compile_error_keeps_file_and_message_not_line():
    sig = signature(MAVEN_COMPILE)
    assert sig == "build:acme/Calc.java: cannot find symbol"
    moved = MAVEN_COMPILE.replace("[12,17]", "[40,3]").replace("run-8f3a", "run-99zz")
    assert signature(moved) == sig


def test_gradle_test_failure():
    assert signature(GRADLE_TEST) == "gradle:CalcTest > addsTwoNumbers()"


def test_jest_failure_names_file_and_case_without_timings():
    sig = signature(JEST)
    assert "js:src/sum.test.js" in sig
    assert "js:adds numbers" in sig
    assert signature(JEST.replace("(5 ms)", "(812 ms)")) == sig


def test_vitest_cross_mark():
    out = " × adds numbers 3ms\n FAIL  src/a.test.ts > sum > adds numbers\n"
    sig = signature(out)
    assert "js:adds numbers" in sig
    assert "src/a.test.ts" in sig


def test_tsc_both_formats_read_the_same():
    assert signature(TSC) == signature(TSC_PRETTY)
    f = failure_of(TSC)
    assert f.count == 2
    assert "TS2304" in f.signature and "12" not in f.signature


def test_go_test_and_go_build():
    assert "go:TestAdd" in signature(GO_TEST)
    assert signature(GO_TEST.replace("0.00s", "0.31s").replace("0.011s", "2.4s")) \
        == signature(GO_TEST)
    assert signature(GO_BUILD) == "build:./calc.go: undefined: totl".replace("./", "")


def test_python_traceback_keeps_frame_and_exception_not_paths_or_addresses():
    sig = signature(PY_TRACEBACK)
    assert sig.startswith("py:app/main.py:run: KeyError")
    other_tmp = PY_TRACEBACK.replace("tmpab12cd", "tmpzz99yy").replace("0x7f3a2b", "0x1")
    assert signature(other_tmp) == sig
    assert signature(PY_TRACEBACK.replace("KeyError", "TypeError")) != sig


def test_npm_errors_skip_the_log_path_noise():
    sig = signature(NPM)
    assert sig.startswith("npm:")
    assert "_logs" not in sig and "ELIFECYCLE" not in sig
    later = NPM.replace("2026-09-26T10_11_12_123Z", "2026-09-27T01_02_03_999Z")
    assert signature(later) == sig


def test_rustc_error_with_its_location_line():
    sig = signature(RUSTC)
    assert sig.startswith("build:src/main.rs: [E0425]")
    assert "cannot find value" in sig


def test_first_error_line_is_the_last_resort():
    out = "compiling…\nfatal: could not read Username for 'https://github.com'\n"
    f = failure_of(out)
    assert f.signature.startswith("line:fatal: could not read Username")
    assert f.count == 1


def test_a_summary_that_reports_no_errors_is_not_an_error_line():
    assert signature("Checked 12 files, 0 errors\nall good\n") == ""


def test_loose_numbers_and_durations_are_masked_in_the_fallback():
    a = "Error: request failed after 1200ms with status 503 at 10:11:12"
    b = "Error: request failed after 900ms with status 503 at 11:02:59"
    assert signature(a) == signature(b)


def test_test_ids_win_over_a_traceback_in_the_same_output():
    out = PY_TRACEBACK + PYTEST_A
    assert signature(out) == "test:tests/test_calc.py::test_add"


def test_result_text_and_signature_of():
    res = {"ok": False, "stdout": "FAILED tests/a.py::t - x\n", "stderr": "warn"}
    assert "FAILED" in result_text(res)
    assert signature_of("", res, {"error": None}) == "test:tests/a.py::t"
    assert result_text("plain") == "plain"


@pytest.mark.parametrize("out", [
    "===== 12 passed in 0.40s =====",
    "Tests:       4 passed, 4 total",
    "[INFO] BUILD SUCCESS",
    "BUILD SUCCESSFUL in 3s",
    "ok  \tgithub.com/acme/calc\t0.011s",
    "test result: ok. 3 passed; 0 failed",
    "Ran 3 tests in 0.001s\n\nOK",
])
def test_runner_green_evidence(out):
    assert runner_green(out)


@pytest.mark.parametrize("out", [
    "",
    "done",                                          # a pipe's own output
    "1 failed, 11 passed in 0.4s",
    "11 passed, 2 errors in 0.4s",
    PYTEST_A,
    MAVEN_TEST,
    JEST,
    GO_TEST,
    "test result: FAILED. 2 passed; 1 failed",
])
def test_runner_green_rejects_failure_or_no_evidence(out):
    assert not runner_green(out)
