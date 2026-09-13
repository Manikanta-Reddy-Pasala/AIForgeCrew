"""Job-test isolation.

``scheduler._RUNNING`` is a module-global set of in-flight job ids, and every
test here gets a FRESH jobs.db (see each module's ``_tmp_db``), so job ids
restart at 1 in every test. One test that ends while its worker thread is
still running therefore marks id 1 "in flight" for every test that follows —
and ``close_job`` deliberately leaves a workspace alone while its agent owns
it, so the symptom lands in a different file entirely.

The leak itself belongs in the test that causes it (and is fixed there). This
fixture is the guard: state that outlives a test does not reach the next one.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_leaked_running_jobs():
    from aiforge_core.jobs import scheduler
    with scheduler._RUNNING_LOCK:
        scheduler._RUNNING.clear()
    yield
    with scheduler._RUNNING_LOCK:
        scheduler._RUNNING.clear()
