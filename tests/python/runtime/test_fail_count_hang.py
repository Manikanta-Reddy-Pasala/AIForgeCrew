"""A test run cut off by its time limit is the worst result, never 0 failing."""
import pytest

from aiforge_core.runtime.parallel_subtasks import _fail_count


@pytest.mark.parametrize("out,n", [
    ("the test run did not finish in 300 s — a test hangs", 999),
    ("tests/test_p.py ..\n+++++++++++++++++ Timeout +++++++++++++++++\n", 999),
    ("E   Failed: Timeout >20.0s\n1 failed in 21.0s", 999),
    ("12 passed in 0.3s", 0),
    ("2 failed, 10 passed in 1.0s", 2),
    ("1 failed, 3 errors in 2s", 4),
    ("Traceback (most recent call last):\nImportError: x", 999),
])
def test_fail_count(out, n):
    assert _fail_count(out) == n
