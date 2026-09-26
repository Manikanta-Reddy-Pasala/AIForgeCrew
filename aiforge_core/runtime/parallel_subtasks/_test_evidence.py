"""How many tests a test run actually executed, read from the runner's output.

A green exit code is not "all tests pass": pytest with nothing to collect, a
tree with no test runner, or a check that ran somewhere else all end "ok"
with ZERO tests run. The team verdict used to print "✅ Built — all tests
pass" for exactly those. The verdict now needs the runner's own summary line
with a count above zero.

Each runner prints its count its own way, and the obvious readings double
count or undercount:

* cargo prints one ``test result:`` line per test binary — the last one is
  usually the doc-tests' ``0 passed``; every line is summed.
* maven surefire prints ``Tests run:`` per class AND a total after
  ``Results:``; only the totals are taken (per-class lines when there is none).
* ``go test`` prints ``ok <pkg>`` per PACKAGE; tests are counted from
  ``--- PASS`` / ``--- FAIL`` lines (``-v``), else the count is unknown.
* dotnet / gradle print ``Passed: 5`` / ``Total: 6`` / ``6 tests completed``.
"""
from __future__ import annotations

import re

# Bounded quantifiers on a single line — the output is arbitrary subprocess
# text.
_CARGO_RE = re.compile(
    r"^test result: \w{2,6}\. (\d{1,7}) passed; (\d{1,7}) failed", re.M)
_JUNIT_LINE_RE = re.compile(
    r"^[^\n]{0,40}Tests run:[ \t]{0,4}(\d{1,7}),[ \t]{0,4}Failures:[ \t]{0,4}"
    r"(\d{1,7})(?:,[ \t]{0,4}Errors:[ \t]{0,4}(\d{1,7}))?([^\n]{0,300})$", re.M)
_JUNIT4_OK_RE = re.compile(r"^OK \((\d{1,7}) tests?\)", re.M)
_UNITTEST_RE = re.compile(r"^Ran (\d{1,7}) tests?\b", re.M)
_GO_CASE_RE = re.compile(r"^--- (PASS|FAIL): ", re.M)
_GO_PKG_RE = re.compile(r"^(ok|FAIL)[ \t]{1,8}\S{1,300}[ \t][^\n]{0,80}$", re.M)
_DOTNET_TOTAL_RE = re.compile(r"\bTotal(?: tests)?:[ \t]{0,8}(\d{1,7})")
_DOTNET_FAILED_RE = re.compile(r"\bFailed:[ \t]{0,8}(\d{1,7})")
_DOTNET_PASSED_RE = re.compile(r"\bPassed:[ \t]{0,8}(\d{1,7})")
_GRADLE_RE = re.compile(
    r"\b(\d{1,7}) tests? completed(?:, (\d{1,7}) failed)?")
_NO_TESTS = ("no tests ran", "collected 0 items", "no tests found",
             "no test files", "0 tests collected", "Ran 0 tests")


def _count_before(output: str, words: tuple[str, ...]) -> int | None:
    from ._reconcile._testrun import _count_before as cb
    return cb(output, words)


def _cargo(out: str):
    m = _CARGO_RE.findall(out)
    if not m:
        return None
    return sum(int(p) + int(f) for p, f in m), sum(int(f) for _p, f in m)


def _junit(out: str):
    rows = _JUNIT_LINE_RE.findall(out)
    if not rows:
        ok = _JUNIT4_OK_RE.findall(out)
        return (sum(int(x) for x in ok), 0) if ok else None
    totals = [r for r in rows
              if "Time elapsed" not in r[3] and " in " not in r[3]
              and "<<<" not in r[3]]
    use = totals or rows
    return (sum(int(r[0]) for r in use),
            sum(int(r[1]) + int(r[2] or 0) for r in use))


def _dotnet(out: str):
    tot = _DOTNET_TOTAL_RE.findall(out)
    if tot:
        failed = _DOTNET_FAILED_RE.findall(out)
        return int(tot[-1]), int(failed[-1]) if failed else 0
    passed = _DOTNET_PASSED_RE.findall(out)
    if passed and ("Failed:" in out or "Skipped:" in out):
        failed = _DOTNET_FAILED_RE.findall(out)
        return (int(passed[-1]) + (int(failed[-1]) if failed else 0),
                int(failed[-1]) if failed else 0)
    return None


def _gradle(out: str):
    m = _GRADLE_RE.findall(out)
    if not m:
        return None
    return sum(int(a) for a, _ in m), sum(int(b or 0) for _, b in m)


def _go(out: str):
    cases = _GO_CASE_RE.findall(out)
    if cases:
        return len(cases), sum(1 for c in cases if c == "FAIL")
    return None


def _structured(out: str):
    """(executed, failed) from a runner whose summary is not pytest-shaped."""
    for reader in (_cargo, _junit, _dotnet, _gradle, _go):
        got = reader(out)
        if got is not None:
            return got
    return None


def go_packages_passed(output: str | None) -> int:
    """``ok <pkg>`` lines that ran tests: go test without ``-v`` — tests ran,
    but the runner never said how many."""
    return sum(1 for m in _GO_PKG_RE.finditer(str(output or ""))
               if m.group(1) == "ok" and "[no test files]" not in m.group(0))


def executed_tests(output: str | None) -> int | None:
    """Tests the run reports as executed (passed + failed + errored), or
    ``None`` when the output carries no recognisable runner summary. ``0`` is
    a definite "the runner ran and found nothing"."""
    out = str(output or "")
    if not out.strip():
        return None
    got = _structured(out)
    if got is not None:
        return got[0]
    passed = _count_before(out, ("passed",))     # pytest, jest, vitest
    failed = _count_before(out, ("failed",))
    errors = _count_before(out, ("error", "errors"))
    if passed is not None or failed is not None:
        return (passed or 0) + (failed or 0) + (errors or 0)
    m = _UNITTEST_RE.findall(out)
    if m:
        return sum(int(x) for x in m)
    low = out.lower()
    if any(k.lower() in low for k in _NO_TESTS):
        return 0
    return None


def failed_tests(output: str | None) -> int | None:
    """Failed + errored tests the run reports, or ``None`` when unreadable."""
    out = str(output or "")
    got = _structured(out)
    if got is not None:
        return got[1]
    failed = _count_before(out, ("failed",))
    errors = _count_before(out, ("error", "errors"))
    if failed is None and errors is None:
        return None
    return (failed or 0) + (errors or 0)


__all__ = ["executed_tests", "failed_tests", "go_packages_passed"]
