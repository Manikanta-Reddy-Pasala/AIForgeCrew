"""What the chat shows while the model writes: the answer streams into the
bubble; a tool step being written and the model's reasoning are a muted draft.
"""
import queue

from aiforge_core.runtime.chat_agent._context import _generation as gen


def _feed(shaper, *items):
    q = queue.SimpleQueue()
    for it in items:
        q.put(it)
    return shaper.drain(q)


def _answer(events):
    return "".join(e["text"] for e in events if e.get("phase") == "answer")


def test_a_native_plain_answer_streams():
    s = gen._DeltaShaper()
    ev = _feed(s, ("content", "The ans"), ("content", "wer is 42."))
    assert ev[0] == {"type": "delta", "phase": "reset"}
    assert _answer(ev) == "The answer is 42."


def test_text_protocol_streams_only_after_final():
    s = gen._DeltaShaper()
    ev = _feed(s, ("content", "THOUGHT: I know this.\n"))
    assert _answer(ev) == ""
    assert any(e["phase"] == "draft" for e in ev)
    ev = _feed(s, ("content", "FINAL: Paris"), ("content", " is the capital."))
    assert _answer(ev) == "Paris is the capital."


def test_a_tool_step_never_reaches_the_bubble():
    s = gen._DeltaShaper()
    ev = _feed(s, ("content", "ACT"), ("content", "ION: file_read\nARGS_JSON: {}"))
    assert _answer(ev) == ""


def test_think_blocks_and_reasoning_are_thinking_not_answer():
    s = gen._DeltaShaper()
    ev = _feed(s, ("reasoning", "hmm"), ("content", "<think>scratch"))
    assert [e["phase"] for e in ev] == ["reset", "thinking", "draft"]
    ev = _feed(s, ("content", "</think>Done."))
    assert _answer(ev) == "Done."


def test_a_retry_restarts_the_text():
    s = gen._DeltaShaper()
    _feed(s, ("start", ""), ("content", "partial ans"))
    ev = _feed(s, ("start", ""), ("content", "fresh"))
    assert ev[0]["phase"] == "reset"
    assert _answer(ev) == "fresh"


def test_complete_live_yields_deltas_then_returns_the_completion(monkeypatch):
    from aiforge_core.llm.client import _http
    from aiforge_core.runtime import chat_cancel
    monkeypatch.setattr(chat_cancel, "is_cancelled", lambda sid: False)

    def complete_fn(role, convo):
        sink = _http._DELTA_SINK.get()
        for piece in ("FINAL: ", "hel", "lo"):
            sink("content", piece)
        return "FINAL: hello"

    g = gen._complete_live(complete_fn, "chat", [], 1)
    events = []
    try:
        while True:
            events.append(next(g))
    except StopIteration as stop:
        out = stop.value
    assert out == "FINAL: hello"
    assert _answer(events) == "hello"


def test_streaming_can_be_switched_off(monkeypatch):
    from aiforge_core.runtime import chat_cancel
    monkeypatch.setattr(chat_cancel, "is_cancelled", lambda sid: False)
    monkeypatch.setenv("AIFORGE_CHAT_STREAM", "0")
    g = gen._complete_live(lambda r, c: "FINAL: x", "chat", [], 1)
    try:
        next(g)
        raise AssertionError("yielded an event with streaming off")
    except StopIteration as stop:
        assert stop.value == "FINAL: x"


# ── team mode streams too ─────────────────────────────────────────────────

def _part(text=None, thought=False):
    import types
    return types.SimpleNamespace(text=text, thought=thought, function_call=None,
                                 function_response=None)


def _event(*parts, partial=True, author="doer"):
    import types
    return types.SimpleNamespace(author=author, partial=partial,
                                 content=types.SimpleNamespace(parts=list(parts)))


def test_a_streamed_team_chunk_is_a_draft_delta():
    from aiforge_core.runtime import chat_pipeline as cp
    ev = cp.partial_events(_event(_part("Writing the parser"), _part("hmm", thought=True)))
    assert ev == [
        {"type": "delta", "role": "doer", "text": "Writing the parser", "phase": "draft"},
        {"type": "delta", "role": "doer", "text": "hmm", "phase": "thinking"},
    ]


def test_team_streaming_is_opt_in(monkeypatch):
    """Off by default: ADK's streamed call skips the escalating wrapper's
    retries, fallback chain and spend recording."""
    import pytest
    pytest.importorskip("google.adk.agents.run_config")
    from google.adk.agents.run_config import StreamingMode

    from aiforge_core.runtime import chat_pipeline as cp
    monkeypatch.delenv("AIFORGE_CHAT_TEAM_STREAM", raising=False)
    assert cp._team_streaming() == {}
    monkeypatch.setenv("AIFORGE_CHAT_TEAM_STREAM", "1")
    assert cp._team_streaming() == {"streaming_mode": StreamingMode.SSE}


def test_partial_chunks_never_become_steps_or_the_answer():
    """The finished event repeats the text; counting chunks too would duplicate
    every step and make the last chunk the 'final' answer."""
    import asyncio
    import queue as _queue

    from aiforge_core.runtime import chat_pipeline as cp

    async def agen():
        yield _event(_part("Hel"), partial=True)
        yield _event(_part("lo"), partial=True)
        yield _event(_part("Hello"), partial=False)

    q = _queue.Queue()
    steps: list = []
    out = asyncio.run(cp._drive_run_events(agen(), None, q, None, None, steps))
    events = [q.get_nowait() for _ in range(q.qsize())]
    assert [e["type"] for e in events].count("delta") == 2
    assert [e for e in events if e["type"] == "thought"][0]["text"] == "Hello"
    assert out["final"] == "Hello"


def test_the_perf_plugin_times_the_whole_streamed_call():
    import asyncio
    import types

    import pytest
    pytest.importorskip("google.adk.plugins.base_plugin")
    from aiforge_core.runtime.perf_plugin import PerfPlugin
    p = PerfPlugin()
    ctx = types.SimpleNamespace(invocation_id="i", agent_name="doer")
    key = p._model_key(ctx)

    async def run():
        await p.before_model_callback(callback_context=ctx, llm_request=None)
        await p.after_model_callback(callback_context=ctx,
                                     llm_response=types.SimpleNamespace(partial=True))
        still_timing = key in p._started
        await p.after_model_callback(callback_context=ctx,
                                     llm_response=types.SimpleNamespace(partial=False))
        return still_timing, key in p._started
    assert asyncio.run(run()) == (True, False)


# ── a re-attaching client replays the stream tail, not every chunk ────────

def test_the_replay_buffer_keeps_only_the_unsettled_stream():
    from aiforge_core.runtime.chat_runs import _Run
    run = _Run(1)
    run.publish({"type": "delta", "phase": "reset"})
    for piece in ("a", "b", "c"):
        run.publish({"type": "delta", "phase": "answer", "text": piece})
    run.publish({"type": "tool", "name": "file_read"})          # settles call 1
    run.publish({"type": "delta", "phase": "reset"})
    run.publish({"type": "delta", "phase": "draft", "text": "THOUGHT: "})
    run.publish({"type": "delta", "phase": "draft", "text": "next"})
    q = run.subscribe()
    replay = [q.get_nowait() for _ in range(q.qsize())]
    assert replay == [
        {"type": "tool", "name": "file_read"},
        {"type": "delta", "phase": "reset"},
        {"type": "delta", "phase": "draft", "text": "THOUGHT: next"},
    ]
    assert all(e["type"] != "delta" for e in run.events)


def test_live_subscribers_still_get_every_chunk():
    from aiforge_core.runtime.chat_runs import _Run
    run = _Run(1)
    q = run.subscribe()
    for piece in ("x", "y"):
        run.publish({"type": "delta", "phase": "answer", "text": piece})
    assert [q.get_nowait()["text"] for _ in range(q.qsize())] == ["x", "y"]


# ── a message right after `done` waits for the bookkeeping, not a 409 ─────

def test_an_answered_run_is_waited_for_not_refused():
    import threading

    from aiforge_core.runtime import chat_runs
    run = chat_runs._Run(77)
    chat_runs._RUNS[77] = run
    try:
        run.publish({"type": "done"})                       # answer is out
        threading.Timer(0.2, run.finish).start()           # bookkeeping ends
        assert chat_runs.settle(77, timeout=5) is True
    finally:
        chat_runs._RUNS.pop(77, None)


def test_a_run_still_working_is_refused():
    from aiforge_core.runtime import chat_runs
    run = chat_runs._Run(78)
    chat_runs._RUNS[78] = run
    try:
        run.publish({"type": "thought", "text": "working"})
        assert chat_runs.settle(78, timeout=5) is False     # no wait at all
    finally:
        chat_runs._RUNS.pop(78, None)
