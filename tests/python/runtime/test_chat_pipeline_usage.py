"""Team/pipeline turns emit the same `usage` context meter as simple chat."""
from __future__ import annotations

import queue
from types import SimpleNamespace

from aiforge_core.llm import call_meter
from aiforge_core.runtime import chat_pipeline_usage as cpu


def _event(author, text="hello world"):
    part = SimpleNamespace(text=text, function_call=None, function_response=None)
    return SimpleNamespace(author=author, content=SimpleNamespace(parts=[part]),
                           partial=False)


def test_usage_event_has_simple_chat_shape_and_stage(monkeypatch):
    call_meter.reset_all()
    ev = cpu.usage_event(None, "doer", est_chars=4000)
    assert ev["type"] == "usage" and ev["stage"] == "doer"
    assert ev["context_tokens"] == 1000          # chars/4 estimate, no session
    assert ev["tokens_reported"] is False
    for k in ("window_tokens", "compact_at_tokens", "compact_pct", "pct",
              "window_source", "llm_turn", "llm_per_min", "context_chars",
              "budget_chars"):
        assert k in ev
    assert ev["window_tokens"] > 0


def test_usage_event_prefers_provider_reported_prompt_tokens():
    call_meter.reset_all()
    tok = call_meter.turn_reset("s1")
    cv = call_meter.bind_turn(tok)
    try:
        t = call_meter.record("doer", "s1")
        call_meter.record_tokens("doer", prompt_tokens=38000, completion_tokens=5,
                                 token=t)
        t = call_meter.record("doer", "s1")
        call_meter.record_tokens("doer", prompt_tokens=41000, completion_tokens=5,
                                 token=t)
    finally:
        call_meter.reset_turn(cv)
    ev = cpu.usage_event("s1", "doer", est_chars=40)
    assert ev["context_tokens"] == 41000 and ev["tokens_reported"] is True
    assert call_meter.snapshot("s1")["last_prompt_tokens"] == 41000
    # A new turn starts from zero again: no stale reading from the old turn.
    call_meter.turn_reset("s1")
    assert call_meter.snapshot("s1")["last_prompt_tokens"] == 0


def test_pipeline_usage_throttles_but_stage_change_always_reports():
    call_meter.reset_all()
    m = cpu.PipelineUsage(None, min_interval=3600)
    assert m.tick("planner") is not None
    assert m.tick("planner") is None              # same stage, throttled
    assert m.tick("doer")["stage"] == "doer"      # stage change goes through
    assert m.tick("doer", force=True) is not None  # completion forces one


def test_emit_for_adk_event_puts_usage_on_queue():
    call_meter.reset_all()
    q: queue.Queue = queue.Queue()
    m = cpu.PipelineUsage(None, min_interval=3600)
    cpu.emit_for_adk_event(q, m, _event("planner", "x" * 400))
    cpu.emit_for_adk_event(q, m, _event("doer", "y" * 400))
    got = [q.get_nowait() for _ in range(q.qsize())]
    assert [e["stage"] for e in got] == ["planner", "doer"]
    assert got[1]["context_tokens"] == 200       # 800 chars accumulated / 4


def test_meter_failure_never_raises(monkeypatch):
    monkeypatch.setattr(call_meter, "snapshot",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("x")))
    assert cpu.usage_event("s", "doer") is None


def test_parallel_stream_reports_after_each_subtask():
    from aiforge_core.runtime.parallel_subtasks import _stream
    src = [{"type": "subtask_update", "slug": "a", "status": "done"},
           {"type": "subtask_update", "slug": "b", "status": "done"}]
    out = list(_stream._with_usage(iter(src), None))
    kinds = [e["type"] for e in out]
    assert kinds[0] == "usage"                      # at start
    assert kinds.count("usage") == 3                # + after each settle
    assert [e for e in out if e["type"] == "subtask_update"] == src
