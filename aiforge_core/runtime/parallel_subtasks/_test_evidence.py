"""How many tests a test run actually executed, read from the runner's output.

A green exit code is not "all tests pass": pytest with nothing to collect, a
tree with no test runner, or a check that ran somewhere else all end "ok"
with ZERO tests run. The team verdict used to print "✅ Built — all tests
pass" for exactly those. The verdict now needs the runner's own summary line
with a count above zero.
"""
from __future__ import annotations

import re

# "Tests run: 5, Failures: 0" (maven surefire / gradle), "Ran 3 tests"
# (unittest). Bounded quantifiers on a single line — the output is arbitrary
# subprocess text.
_JUNIT_RE = re.compile(r"Tests run:[ \t]{0,4}(\d{1,7})")
_UNITTEST_RE = re.compile(r"^Ran (\d{1,7}) tests?\b", re.M)
# go test: one "ok  <pkg>  0.01s" line per package whose tests passed
# ("ok ... [no test files]" ran nothing).
_GO_OK_RE = re.compile(r"^ok[ \t]{1,8}\S{1,300}[ \t][^\n]{0,80}$", re.M)
_NO_TESTS = ("no tests ran", "collected 0 items", "no tests found",
             "no test files", "0 tests collected", "Ran 0 tests")


def _count_before(output: str, words: tuple[str, ...]) -> int | None:
    from ._reconcile._testrun import _count_before as cb
    return cb(output, words)


def executed_tests(output: str | None) -> int | None:
    """Tests the run reports as executed (passed + failed + errored), or
    ``None`` when the output carries no recognisable runner summary. ``0`` is
    a definite "the runner ran and found nothing"."""
    out = str(output or "")
    if not out.strip():
        return None
    passed = _count_before(out, ("passed",))     # pytest, jest, cargo, vitest
    failed = _count_before(out, ("failed",))
    errors = _count_before(out, ("error", "errors"))
    if passed is not None or failed is not None:
        return (passed or 0) + (failed or 0) + (errors or 0)
    m = _JUNIT_RE.findall(out)
    if m:
        return sum(int(x) for x in m)
    m = _UNITTEST_RE.findall(out)
    if m:
        return sum(int(x) for x in m)
    go = [ln for ln in _GO_OK_RE.findall(out) if "[no test files]" not in ln]
    if go:
        return len(go)
    low = out.lower()
    if any(k.lower() in low for k in _NO_TESTS):
        return 0
    return None


def failed_tests(output: str | None) -> int | None:
    """Failed + errored tests the run reports, or ``None`` when unreadable."""
    out = str(output or "")
    failed = _count_before(out, ("failed",))
    errors = _count_before(out, ("error", "errors"))
    if failed is None and errors is None:
        return None
    return (failed or 0) + (errors or 0)


__all__ = ["executed_tests", "failed_tests"]
