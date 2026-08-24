"""Tests for the live chat-run registry (chat_runs) — the buffer + pub/sub that
lets an in-flight chat turn survive the client navigating away and back.

Covers: buffer replay on late subscribe, live fan-out to multiple subscribers,
finish waking everyone, is_running lifecycle, subscribe to an already-finished
run, and a newer run evicting an older one for the same session.
"""
import queue
import threading

import pytest

from aiforge_core.runtime import chat_runs as cr


@pytest.fixture(autouse=True)
def _clean():
    # Drop any registry state from other tests for the session ids used here.
    for sid in (101, 102, 103, 104):
        cr._RUNS.pop(sid, None)
    yield
    for sid in (101, 102, 103, 104):
        cr._RUNS.pop(sid, None)


def _drain(q: queue.Queue) -> list:
    out = []
    while True:
        item = q.get_nowait() if not q.empty() else None
        if item is None:
            break
        out.append(item)
    return out


def test_late_subscriber_replays_full_buffer():
    run = cr.start(101)
    run.publish({"type": "thought", "text": "a"})
    run.publish({"type": "tool", "name": "editor"})
    # Subscribe AFTER events were published — must replay them all in order.
    q = run.subscribe()
    got = _drain(q)
    assert [e["type"] for e in got] == ["thought", "tool"]


def test_live_fanout_to_multiple_subscribers():
    run = cr.start(102)
    q1 = run.subscribe()
    q2 = run.subscribe()
    run.publish({"type": "message", "text": "hi"})
    assert q1.get_nowait()["text"] == "hi"
    assert q2.get_nowait()["text"] == "hi"


def test_finish_pushes_sentinel_and_clears_running():
    run = cr.start(103)
    q = run.subscribe()
    assert cr.is_running(103) is True
    run.publish({"type": "done"})
    run.finish()
    assert cr.is_running(103) is False
    items = []
    while True:
        it = q.get_nowait()
        items.append(it)
        if it is cr._SENTINEL:
            break
    assert items[0]["type"] == "done"
    assert items[-1] is cr._SENTINEL


def test_subscribe_to_finished_run_replays_then_sentinel():
    run = cr.start(104)
    run.publish({"type": "message", "text": "done already"})
    run.publish({"type": "done"})
    run.finish()
    # A client that re-attaches right at the finish line still gets the buffer.
    q = run.subscribe()
    items = []
    while True:
        it = q.get_nowait()
        items.append(it)
        if it is cr._SENTINEL:
            break
    assert items[0]["text"] == "done already"
    assert items[-1] is cr._SENTINEL


def test_new_run_evicts_old_for_same_session():
    old = cr.start(101)
    new = cr.start(101)
    assert cr.get(101) is new
    assert cr.get(101) is not old
    # Finishing the OLD run object must not mark the NEW one done.
    old.finish()
    assert cr.is_running(101) is True


def test_publish_after_finish_is_dropped():
    run = cr.start(102)
    run.finish()
    run.publish({"type": "thought", "text": "late"})   # ignored
    q = run.subscribe()
    # Only the sentinel — no late event leaked into the buffer.
    assert q.get_nowait() is cr._SENTINEL
    assert q.empty()


def test_iter_subscription_yields_until_sentinel():
    run = cr.start(103)
    q = run.subscribe()

    def producer():
        run.publish({"type": "thought", "text": "x"})
        run.publish({"type": "done"})
        run.finish()

    threading.Thread(target=producer, daemon=True).start()
    got = list(cr.iter_subscription(run, q, ping_every=2.0))
    types = [e["type"] for e in got]
    assert "thought" in types
    assert "done" in types
    # The subscriber is removed from the run on generator close.
    assert q not in run.subscribers
