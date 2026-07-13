"""_fail_count must sum pytest FAILED + ERRORS — counting only 'failed' made
the reconcile stop early (it saw '1 failing' while 10 errors remained)."""
from aiforge_core.runtime.parallel_subtasks import _fail_count


def test_failed_plus_errors_summed():
    out = "1 failed, 24 passed, 10 errors in 0.63s"
    assert _fail_count(out) == 11


def test_errors_only():
    assert _fail_count("=== 10 errors in 0.5s ===") == 10


def test_failed_only():
    assert _fail_count("5 failed, 3 passed in 1s") == 5


def test_all_green():
    assert _fail_count("24 passed in 0.4s") == 0


def test_collection_traceback_worst():
    assert _fail_count("Traceback (most recent call last):\n  ...\nInterrupted") == 999


def test_single_error_singular():
    assert _fail_count("1 error in 0.2s") == 1
