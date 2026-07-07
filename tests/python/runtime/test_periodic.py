"""One internal recurring-task engine — register + fire on interval/daily,
debounced, launched through background.spawn.
"""
from __future__ import annotations

import time
from datetime import datetime


def test_register_and_fire_interval(monkeypatch):
    from aiforge_core.runtime import periodic as p
    # isolate the module-global task list
    monkeypatch.setattr(p, "_TASKS", [])
    monkeypatch.setattr(p, "_started", False)
    fired = []
    p.register("unit", lambda: fired.append(1), every_s=0.5, debounce_s=0.0)
    p.start()
    for _ in range(40):
        if fired:
            break
        time.sleep(0.1)
    assert fired


def test_register_is_idempotent(monkeypatch):
    from aiforge_core.runtime import periodic as p
    monkeypatch.setattr(p, "_TASKS", [])
    p.register("dup", lambda: None, every_s=10)
    p.register("dup", lambda: None, every_s=10)
    assert sum(1 for t in p._TASKS if t.name == "dup") == 1


def test_at_hour_due_calc():
    from aiforge_core.runtime import periodic as p
    now = datetime.now()
    t = p._Task("x", lambda: None, at_hour=now.hour)
    # same hour already started → next fire is ~tomorrow (positive, < 25h)
    s = t._next_after(0.0, now)
    assert 0 <= s <= 25 * 3600


def test_disabled(monkeypatch):
    from aiforge_core.runtime import periodic as p
    monkeypatch.setenv("AIFORGE_PERIODIC_DISABLE", "1")
    monkeypatch.setattr(p, "_TASKS", [])
    monkeypatch.setattr(p, "_started", False)
    p.register("x", lambda: None, every_s=1)
    p.start()
    assert p._started is False        # start() no-op when disabled
