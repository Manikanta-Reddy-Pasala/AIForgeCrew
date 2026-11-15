"""Java, Rust, C++ and Python runs read the same way everywhere memory and the
verdicts look: test-run detection, green/red, fail count, failure signature
(the key the fixes memory files a repair under) and the learned run recipe.

Before: ctest / make test / meson / googletest runs were not test runs, a
green ctest or gtest run was not green, Maven's green summary counted 999
failures, a failing gtest run counted 0, and Rust/ctest/gtest failures were
not filed under the failing test.
"""
import pytest

from aiforge_core.runtime import failure_signature as fs
from aiforge_core.runtime.chat_agent._turn import _outcomes as O
from aiforge_core.runtime.parallel_subtasks import _fail_count

GREEN = {
    "maven": "[INFO] Tests run: 5, Failures: 0, Errors: 0, Skipped: 0\n[INFO] BUILD SUCCESS",
    "cargo": "running 5 tests\ntest result: ok. 5 passed; 0 failed; 0 ignored",
    "ctest": "100% tests passed, 0 tests failed out of 4\n\nTotal Test time (real) =   0.03 sec",
    "gtest": "[==========] 5 tests from 2 test suites ran. (1 ms total)\n[  PASSED  ] 5 tests.",
    "meson": "Ok:                 4\nExpected Fail:      0\nFail:               0",
    "pytest": "===== 5 passed in 0.12s =====",
}
RED = {
    "maven": ("[ERROR] Tests run: 5, Failures: 1, Errors: 0, Skipped: 0\n[ERROR] Failures:\n"
              "[ERROR]   FooTest.adds:12 expected: <3> but was: <4>\n[INFO] BUILD FAILURE",
              1, "junit:FooTest.adds"),
    "cargo": ("running 2 tests\ntest tests::adds ... FAILED\n\nfailures:\n"
              "test result: FAILED. 1 passed; 1 failed; 0 ignored", 1, "rust:tests::adds"),
    "ctest": ("75% tests passed, 1 tests failed out of 4\n\nThe following tests FAILED:\n"
              "\t  3 - parser_test (Failed)\nErrors while running CTest", 1, "ctest:parser_test"),
    "gtest": ("[ RUN      ] Parser.Adds\n/w/tests/parser_test.cpp:14: Failure\n"
              "[  FAILED  ] Parser.Adds (0 ms)\n[==========] 3 tests ran.\n[  PASSED  ] 2 tests.\n"
              "[  FAILED  ] 1 test, listed below:\n[  FAILED  ] Parser.Adds\n\n 1 FAILED TEST",
              1, "gtest:Parser.Adds"),
    "pytest": ("FAILED tests/test_a.py::test_adds - assert 3 == 4\n===== 1 failed, 4 passed in 0.1s =====",
               1, "test:tests/test_a.py::test_adds"),
}


@pytest.mark.parametrize("name", list(GREEN))
def test_green_runs(name):
    out = GREEN[name]
    assert fs.runner_green(out) and _fail_count(out) == 0
    assert O._TESTS_RAN.search(out)


@pytest.mark.parametrize("name", list(RED))
def test_red_runs(name):
    out, fails, sig = RED[name]
    assert not fs.runner_green(out)
    assert _fail_count(out) == fails
    assert fs.failure_of(out).signature == sig


def test_cxx_link_error_is_filed_by_symbol():
    out = ("main.cpp:(.text+0x9): undefined reference to `Parser::parse()'\n"
           "collect2: error: ld returned 1 exit status")
    assert fs.failure_of(out).signature == "link:undefined reference to Parser::parse()"


@pytest.mark.parametrize("cmd", [
    "ctest --test-dir build", "cmake --build build && ctest --test-dir build",
    "make test", "make -C build check", "meson test -C builddir",
    "./build/unit_tests --gtest_filter=Foo.*", "cargo nextest run", "cargo test",
    "./mvnw test", "./gradlew test", ".venv/bin/python -m pytest",
])
def test_every_toolchain_counts_as_a_test_run(cmd):
    assert O._TEST_CMD.search(cmd)


def test_narrowed_cxx_runs_are_narrow():
    assert O._NARROW.search(" ./build/unit_tests --gtest_filter=Foo.*")
    assert O._NARROW.search(" ctest --test-dir build -R parser")
