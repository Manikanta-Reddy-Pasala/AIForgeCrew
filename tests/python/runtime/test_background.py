"""background.spawn — one bounded-worker helper (daemon thread / detached
process) with a mandatory name + error sink, replacing hand-rolled Thread starts.
"""
from __future__ import annotations

import threading
import time


def test_spawn_thread_runs_and_names():
    from aiforge_core.runtime import background as bg
    done = threading.Event()
    seen = {}
    t = bg.spawn(lambda: (seen.update(name=threading.current_thread().name),
                          done.set()), name="unit-worker")
    assert done.wait(2.0)
    assert seen["name"] == "unit-worker"
    assert t is not None


def test_spawn_thread_error_is_sunk_not_raised():
    from aiforge_core.runtime import background as bg
    got = {}
    ev = threading.Event()

    def _boom():
        raise RuntimeError("boom")

    bg.spawn(_boom, name="boomer",
             on_error=lambda e: (got.update(err=str(e)), ev.set()))
    assert ev.wait(2.0)
    assert got["err"] == "boom"          # error surfaced, not swallowed silently


def test_spawn_process_needs_argv():
    from aiforge_core.runtime import background as bg
    assert bg.spawn(name="x", kind="process") is None      # no argv → None, no raise


def test_spawn_process_launches():
    import sys
    from aiforge_core.runtime import background as bg
    h = bg.spawn(name="sleeper", kind="process",
                 argv=[sys.executable, "-c", "pass"])
    assert h is not None
    for _ in range(50):
        if h.poll() is not None:
            break
        time.sleep(0.02)
    assert h.poll() == 0
