"""The model summary is written behind the turn, per RUN, and lands in the
breadcrumb it was written for."""
import threading
import time

import pytest


def _long_convo(tag="x"):
    convo = [{"role": "system", "content": "S" * 100}]
    for _ in range(30):
        convo.append({"role": "assistant",
                      "content": "THOUGHT: t\nACTION: file_read\nARGS_JSON: {}"})
        convo.append({"role": "user", "content": "OBSERVATION: " + tag * 200})
    return convo


def _grow(convo, n=20, tag="y"):
    convo = list(convo)
    for _ in range(n):
        convo.append({"role": "assistant",
                      "content": "THOUGHT: t\nACTION: grep\nARGS_JSON: {}"})
        convo.append({"role": "user", "content": "OBSERVATION: " + tag * 200})
    return convo


def _wait_for(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def c(monkeypatch):
    from aiforge_core.runtime.chat_agent._context import _compaction, _summary_bg
    monkeypatch.setenv("AIFORGE_COMPACT_MODE", "llm")
    monkeypatch.setenv("AIFORGE_CHAT_CONTEXT_BUDGET_CHARS", "2000")
    _summary_bg.reset()
    yield _compaction
    _summary_bg.reset()


def test_llm_compact_returns_before_the_model_answers(c):
    started, release = threading.Event(), threading.Event()
    seen_role = {}

    def slow(role, _messages):
        seen_role["role"] = role
        started.set()
        release.wait(5)
        return "SUMMARY FROM MODEL"

    try:
        t0 = time.monotonic()
        out = c._compact_convo(_long_convo(), keep_recent=8,
                               complete_fn=slow, run_key="run-behind")
        assert time.monotonic() - t0 < 1.0
        assert "auto-condensed" in out[0]["content"]
        assert "SUMMARY FROM MODEL" not in out[0]["content"]
        assert started.wait(2)
        assert seen_role["role"] == "learner"
    finally:
        release.set()


def test_the_summary_lands_in_its_own_breadcrumb_next_step(c):
    """Not one condense behind: the very next step splices it in, and the
    tool tally stays next to it."""
    def fast(_role, _messages):
        return "- edited foo.py\n- ran tests"

    out = c._compact_convo(_long_convo(), keep_recent=8,
                           complete_fn=fast, run_key="run-splice")
    assert "(condense #1)" in out[0]["content"]
    from aiforge_core.runtime.chat_agent._context import _summary_bg
    assert _wait_for(lambda: _summary_bg.pending("run-splice") is None)
    nxt = c._compact_convo(out, keep_recent=8, complete_fn=fast,
                           run_key="run-splice")
    sys = nxt[0]["content"]
    assert "- edited foo.py" in sys
    assert "Work done so far: file_read" in sys
    assert "(condense #1)" in sys
    assert nxt[1:] == out[1:]                     # only the note changed


def test_parallel_runs_without_a_session_never_share_a_summary(c):
    """session_id is None for every unattended run. Each run's summary must
    reach only its own prompt."""
    def by_tag(_role, messages):
        body = messages[-1]["content"]
        return "SUMMARY-A" if "aaaa" in body else "SUMMARY-B"

    a = c._compact_convo(_long_convo("a"), keep_recent=8, complete_fn=by_tag,
                         session_id=None, run_key="run-a")
    b = c._compact_convo(_long_convo("b"), keep_recent=8, complete_fn=by_tag,
                         session_id=None, run_key="run-b")
    from aiforge_core.runtime.chat_agent._context import _summary_bg
    assert _wait_for(lambda: _summary_bg.pending("run-a") is None
                     and _summary_bg.pending("run-b") is None)
    a2 = c._compact_convo(a, keep_recent=8, complete_fn=by_tag, run_key="run-a")
    b2 = c._compact_convo(b, keep_recent=8, complete_fn=by_tag, run_key="run-b")
    assert "SUMMARY-A" in a2[0]["content"] and "SUMMARY-B" not in a2[0]["content"]
    assert "SUMMARY-B" in b2[0]["content"] and "SUMMARY-A" not in b2[0]["content"]


def test_no_run_key_means_no_background_summary(c):
    called = threading.Event()

    def fn(_role, _messages):
        called.set()
        return "NOPE"

    out = c._compact_convo(_long_convo(), keep_recent=8, complete_fn=fn,
                           session_id=None)
    assert "auto-condensed" in out[0]["content"]
    assert not called.wait(0.3)


def test_a_newer_condense_discards_the_older_summary(c):
    """Generation 1 is still running when generation 2 starts: it is
    cancelled, and its late answer is never spliced anywhere."""
    from aiforge_core.runtime.chat_agent._context import _summary_bg
    gate1 = threading.Event()
    calls = []

    def fn(_role, messages):
        calls.append(messages[-1]["content"])
        if len(calls) == 1:
            gate1.wait(3)
            return "OLD SLICE SUMMARY"
        return "NEW SLICE SUMMARY"

    out1 = c._compact_convo(_long_convo(), keep_recent=8, complete_fn=fn,
                            run_key="run-gen")
    assert _wait_for(lambda: len(calls) == 1)
    out2 = c._compact_convo(_grow(out1), keep_recent=8, complete_fn=fn,
                            run_key="run-gen", force=True)
    assert "(condense #2)" in out2[0]["content"]
    gate1.set()
    assert _wait_for(lambda: _summary_bg.pending("run-gen") is None)
    time.sleep(0.1)
    out3 = c._compact_convo(out2, keep_recent=8, complete_fn=fn,
                            run_key="run-gen")
    assert "NEW SLICE SUMMARY" in out3[0]["content"]
    assert "OLD SLICE SUMMARY" not in out3[0]["content"]
    # A stale generation is refused outright too.
    assert _summary_bg.take("run-gen", 1) == ""


def test_release_cancels_and_forgets(c):
    from aiforge_core.runtime.chat_agent._context import _summary_bg
    hold = threading.Event()
    c._compact_convo(_long_convo(), keep_recent=8,
                     complete_fn=lambda r, m: hold.wait(3) and "LATE",
                     run_key="run-end")
    assert _summary_bg.pending("run-end") == 1
    c.release_run("run-end")
    hold.set()
    assert _summary_bg.pending("run-end") is None
    time.sleep(0.1)
    assert _summary_bg.take("run-end", 1) == ""


def test_background_summary_does_not_use_the_turns_tool_queue(c, monkeypatch):
    """The native complete_fn owns the turn's queued reads. The summary
    must call the plain client instead."""
    called = threading.Event()

    def native(role, _messages):
        called.set()
        return "SHOULD NOT RUN"

    native.take_queued = lambda: None
    seen = {}

    def plain(role, _messages):
        seen["role"] = role
        return "PLAIN SUMMARY"

    monkeypatch.setattr("aiforge_core.llm.client.complete", plain, raising=False)
    out = c._compact_convo(_long_convo(), keep_recent=8,
                           complete_fn=native, run_key="run-native")
    from aiforge_core.runtime.chat_agent._context import _summary_bg
    assert _wait_for(lambda: _summary_bg.pending("run-native") is None)
    out = c._compact_convo(out, keep_recent=8, complete_fn=native,
                           run_key="run-native")
    assert "PLAIN SUMMARY" in out[0]["content"]
    assert seen["role"] == "learner"
    assert not called.is_set()


def test_the_summary_is_not_preempted_by_its_own_turn(c, monkeypatch):
    """The summary rides the compaction rate category, but it is part of the
    turn: it must not sit out the interactive yield window, and the turn's
    next send (abort_background) must not cancel it."""
    from aiforge_core.llm import interactive_gate as gate
    from aiforge_core.llm import rate_limiter as rl
    from aiforge_core.llm.client._http import _CANCEL
    monkeypatch.setenv("AIFORGE_BACKGROUND_YIELD_S", "30")
    gate.reset()
    got = {}

    def plain(role, _messages):
        gate.note_interactive()                # the turn's next chat send
        t0 = time.monotonic()
        rl.acquire_global(role=role, max_wait_s=10)
        got["waited"] = time.monotonic() - t0
        ev = _CANCEL.get()
        got["bound"] = ev is not None
        gate.abort_background()
        got["cancelled"] = bool(ev is not None and ev.is_set())
        return "KEPT SUMMARY"

    native = lambda r, m: "x"                  # noqa: E731
    native.take_queued = lambda: None
    monkeypatch.setattr("aiforge_core.llm.client.complete", plain, raising=False)
    try:
        c._compact_convo(_long_convo(), keep_recent=8, complete_fn=native,
                         run_key="run-exempt")
        from aiforge_core.runtime.chat_agent._context import _summary_bg
        assert _wait_for(lambda: "cancelled" in got, 5)
        assert got["waited"] < 2.0
        assert got["bound"] is True
        assert got["cancelled"] is False
        assert _wait_for(lambda: _summary_bg.pending("run-exempt") is None)
    finally:
        gate.reset()
