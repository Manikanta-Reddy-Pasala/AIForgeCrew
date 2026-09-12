"""Compaction must never fold memory while a chat run is still working.

Found live, not by reading: a team run for session 11 was calling tools at
23:15:05, 23:15:38 and 23:16:25 while the idle compactor ran real LLM folds at
23:15:25 and 23:16:22. Two causes, one test file:

* the SSE producer finished the run as soon as it handed off to the background
  team driver, so minutes of team work looked like an idle box; and
* ``_LAST_ACTIVITY`` was stamped only at start/finish, so a long turn looked
  idle from the moment it began.
"""
from __future__ import annotations

import time

from aiforge_core.runtime import chat_runs


def _fresh_registry(monkeypatch):
    """An empty run registry, so a stray run from another test can't answer."""
    monkeypatch.setattr(chat_runs, "_RUNS", {})
    monkeypatch.setattr(chat_runs, "_LAST_ACTIVITY", [0.0])


def test_publishing_an_event_counts_as_activity(monkeypatch):
    _fresh_registry(monkeypatch)
    run = chat_runs.start(1)
    chat_runs._LAST_ACTIVITY[0] = time.time() - 3600      # a long, quiet turn
    run.publish({"type": "tool", "name": "read_lines"})
    assert time.time() - chat_runs.last_activity() < 5


def test_a_finished_run_publishes_nothing_and_stamps_nothing(monkeypatch):
    _fresh_registry(monkeypatch)
    run = chat_runs.start(2)
    run.finish()
    stamped = chat_runs.last_activity()
    run.publish({"type": "tool", "name": "late"})
    assert chat_runs.last_activity() == stamped


def test_a_registered_run_means_the_box_is_busy_however_old(monkeypatch):
    """The run itself — not the clock — is the authority. A team turn can run
    for an hour; the idle compactor must not start folding at minute ten."""
    from aiforge_core.runtime import compact_idle
    _fresh_registry(monkeypatch)
    monkeypatch.setattr(compact_idle, "_tickets_in_progress", lambda: False)
    chat_runs.start(3)
    chat_runs._LAST_ACTIVITY[0] = time.time() - 86400
    assert compact_idle.user_active() is True
    chat_runs.finish(3)
    chat_runs._LAST_ACTIVITY[0] = time.time() - 86400
    assert compact_idle.user_active() is False


def test_the_producer_leaves_a_driver_owned_run_to_the_driver():
    """Team mode's background driver owns the run's lifetime AND its teardown.

    A source check on purpose: the bug was one unconditional call in a cleanup
    block, and the thing worth pinning is that the call stays guarded."""
    from pathlib import Path
    # chat_runs.py lives in aiforge_core/runtime/, so parents[1] IS aiforge_core.
    src = Path(chat_runs.__file__).resolve().parents[1] / "api" / "routes" / "chat.py"
    text = src.read_text(encoding="utf-8")
    guard = text.index('if not path["driver"]:\n        run.finish()')
    assert guard > 0

    from aiforge_core.runtime import chat_pipeline
    teardown = Path(chat_pipeline.__file__).read_text(encoding="utf-8")
    assert "chat_runs.finish(session_id)" in teardown
