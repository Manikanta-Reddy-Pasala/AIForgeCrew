"""Stop and a typed message cut a chat-agent wait short."""
import threading

import pytest

from aiforge_core.runtime import chat_cancel, chat_interject, run_interrupt


def test_pause_returns_as_soon_as_stop_is_set(monkeypatch):
    chat_cancel.start(77)
    try:
        monkeypatch.setattr(run_interrupt, "reason", lambda sid: "stop")
        assert run_interrupt.pause(30, 77) == "stop"
    finally:
        chat_cancel.finish(77)


def test_stop_wins_over_a_queued_message():
    chat_cancel.start(78)
    chat_interject.push(78, "tear")
    chat_cancel.cancel(78)
    try:
        assert run_interrupt.reason(78) == "stop"
    finally:
        chat_cancel.finish(78)
        chat_interject.clear(78)


def test_an_extra_detail_does_not_replace_a_running_task():
    chat_interject.clear(80)
    chat_interject.push(80, "also name the function add_numbers")
    assert run_interrupt.replaces_running_work(80) is False
    assert run_interrupt.attention(80, only_replace=True) is None
    chat_interject.clear(80)


def test_drop_that_replaces_a_running_task():
    chat_interject.clear(81)
    chat_interject.push(81, "drop the sleep and write the file")
    assert run_interrupt.replaces_running_work(81) is True
    assert run_interrupt.attention(81, only_replace=True) == "steer"
    chat_interject.clear(81)


def test_hard_stop_is_only_an_explicit_stop_phrase():
    cut = run_interrupt.text_cuts_running_work
    for phrase in ("stop that", "cancel it", "abort the run",
                   "drop that", "kill it", "halt now"):
        assert cut(phrase) is True, phrase
    for phrase in ("don't forget the date", "use the API instead",
                   "forget the extra log", "also add a log line",
                   "please continue"):
        assert cut(phrase) is False, phrase


def test_a_watch_only_cuts_on_an_explicit_stop_phrase():
    """only_replace still sees "instead". A watch uses only_cut.
    "don't forget" is a reminder: it replaces nothing and cuts nothing."""
    chat_interject.clear(83)
    chat_interject.push(83, "don't forget the date")
    assert run_interrupt.replaces_running_work(83) is False
    assert run_interrupt.attention(83, only_replace=True) is None
    assert run_interrupt.attention(83, only_cut=True) is None
    chat_interject.clear(83)
    chat_interject.push(83, "use grep instead")
    assert run_interrupt.replaces_running_work(83) is True
    assert run_interrupt.attention(83, only_replace=True) == "steer"
    chat_interject.clear(83)
    chat_interject.push(83, "use grep instead")
    assert run_interrupt.attention(83, only_cut=True) is None
    chat_interject.clear(83)
    chat_interject.push(83, "stop")
    assert run_interrupt.attention(83, only_cut=True) == "steer"
    chat_interject.clear(83)


def test_a_queued_message_is_a_steer_not_a_stop():
    chat_cancel.start(79)
    chat_interject.push(79, "tear")
    try:
        assert run_interrupt.reason(79) == "steer"
        assert run_interrupt.steered()["steered"] is True
    finally:
        chat_cancel.finish(79)
        chat_interject.clear(79)


def test_stop_skips_the_enhancer_model_call(monkeypatch):
    from aiforge_core.llm import client
    from aiforge_core.runtime.parallel_subtasks._planning_enhance import _enhance
    chat_cancel.start(80)
    chat_cancel.cancel(80)
    monkeypatch.setattr(client, "complete",
                        lambda *a, **k: pytest.fail("enhancer ran after Stop"))
    prompt = "please rewrite the login handler so empty passwords are rejected"
    try:
        assert _enhance(prompt, session_id=80) == prompt
    finally:
        chat_cancel.finish(80)


def test_stop_ends_the_memory_wait():
    """The reranker used to hold Stop for its whole timeout."""
    import time
    from concurrent.futures import ThreadPoolExecutor
    from aiforge_core.runtime.chat_agent._context import _recall_prefetch as rp
    release = threading.Event()
    chat_cancel.start(90)
    chat_cancel.cancel(90)
    ex = ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(release.wait, 30)
    args = ("what is 2+2", 6, "repo", 90, ())
    rp._PENDING[90] = (args, fut, time.monotonic())
    try:
        assert rp.take(args) is None
    finally:
        release.set()
        ex.shutdown(wait=False)
        chat_cancel.finish(90)
