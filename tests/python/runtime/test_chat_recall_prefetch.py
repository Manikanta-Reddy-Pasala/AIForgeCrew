"""Session-start recall prefetch: the chat bundle's memory recall is started
while the enhancer's LLM call runs, and the bundle reuses it ONLY when it would
have made the identical call — so what reaches the model is unchanged."""
import threading
import time

import pytest

from aiforge_core.runtime import chat_agent, request_context
from aiforge_core.runtime.chat_agent._context import _recall_prefetch

HITS = [{"text": "we pinned kafka retries to 5", "source": "memory"},
        {"text": "order service owns the DLQ", "source": "keyword"}]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_PROACTIVE_RECALL", "lite")
    from aiforge_core.runtime.chat_agent._context import _window
    monkeypatch.setattr(_window, "_cave_mode", lambda: False)
    monkeypatch.setattr(_window, "_ctx_on", lambda _b: True)
    # No LLM: the ranked-list fallback makes the recall block deterministic.
    from aiforge_core.memory import recall_summary
    monkeypatch.setattr(recall_summary, "summarize_hits", lambda q, h: "")
    _recall_prefetch._PENDING.clear()
    yield
    _recall_prefetch._PENDING.clear()


def _fake_query(calls, delay=0.0, fail=False):
    def q(text, **kw):
        calls.append({"text": text, **kw, "t0": time.monotonic(),
                      "repo_root": request_context.get_repo_root()})
        time.sleep(delay)
        calls[-1]["t1"] = time.monotonic()
        if fail:
            raise RuntimeError("boom")
        return {"hits": [dict(h) for h in HITS]}
    return q


def _msgs(text):
    return [{"role": "user", "content": text}]


def test_prefetched_result_is_used_and_block_is_identical(monkeypatch, tmp_path):
    cwd = str(tmp_path / "shop")
    (tmp_path / "shop").mkdir()
    ask = "fix the kafka retry bug in the order service"
    calls: list = []
    monkeypatch.setattr("aiforge_core.memory.unified_query.query", _fake_query(calls))
    serial = chat_agent._memory_recall(cwd, ask, limit=6, session_id=7)
    assert len(calls) == 1

    calls.clear()
    _recall_prefetch.start(_msgs(ask), cwd, 7)
    # The bundle sees the user's words after the enhancer folded its spec in.
    folded = f"{ask}\n\n---\n[Interpreted request — ...:]\nSPEC"
    q = folded.split("\n\n---\n[Interpreted request")[0].strip()
    prefetched = chat_agent._memory_recall(cwd, q, limit=6, session_id=7)

    assert prefetched == serial                      # byte-identical block
    assert len(calls) == 1                           # recall ran ONCE (prefetch)
    assert calls[0]["exclude_session"] == 7 and calls[0]["limit"] == 6


def test_mismatch_waits_for_prefetch_then_queries_itself(monkeypatch, tmp_path):
    """A different call must not reuse the prefetch — and must not overlap it
    (the reranker sidecar 500s one of two concurrent requests)."""
    cwd = str(tmp_path)
    calls: list = []
    monkeypatch.setattr("aiforge_core.memory.unified_query.query",
                        _fake_query(calls, delay=0.2))
    _recall_prefetch.start(_msgs("first wording of the ask"), cwd, 3)
    chat_agent._memory_recall(cwd, "a different ask", limit=6, session_id=3)
    assert [c["text"] for c in calls] == ["first wording of the ask",
                                          "a different ask"]
    assert calls[1]["t0"] >= calls[0]["t1"]          # serialized, no overlap


def test_prefetch_error_falls_back_to_a_fresh_query(monkeypatch, tmp_path):
    cwd = str(tmp_path)
    calls: list = []
    monkeypatch.setattr("aiforge_core.memory.unified_query.query",
                        _fake_query(calls, fail=True))
    _recall_prefetch.start(_msgs("why does sync stall"), cwd, 4)
    time.sleep(0.05)
    good: list = []
    monkeypatch.setattr("aiforge_core.memory.unified_query.query", _fake_query(good))
    out = chat_agent._memory_recall(cwd, "why does sync stall", limit=6, session_id=4)
    assert len(good) == 1 and "kafka retries" in out


def test_follow_up_turn_does_not_prefetch(monkeypatch, tmp_path):
    calls: list = []
    monkeypatch.setattr("aiforge_core.memory.unified_query.query", _fake_query(calls))
    msgs = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
            {"role": "user", "content": "now do c"}]
    _recall_prefetch.start(msgs, str(tmp_path), 5)
    assert _recall_prefetch._PENDING == {} and calls == []


def test_leftover_from_an_old_turn_is_not_reused(monkeypatch, tmp_path):
    cwd = str(tmp_path)
    calls: list = []
    monkeypatch.setattr("aiforge_core.memory.unified_query.query", _fake_query(calls))
    _recall_prefetch.start(_msgs("same words"), cwd, 6)
    args, fut, _t = _recall_prefetch._PENDING[6]
    _recall_prefetch._PENDING[6] = (args, fut, time.monotonic() - 10_000)
    chat_agent._memory_recall(cwd, "same words", limit=6, session_id=6)
    assert len(calls) == 2


def test_prefetch_carries_the_request_repo_root(monkeypatch, tmp_path):
    """The graphify source reads the request's repo root — the worker thread
    must see the same one the bundle's own query would."""
    calls: list = []
    monkeypatch.setattr("aiforge_core.memory.unified_query.query", _fake_query(calls))
    tok = request_context.set_repo_root(str(tmp_path))
    try:
        _recall_prefetch.start(_msgs("where is the graph"), str(tmp_path), 8)
        _recall_prefetch._PENDING[8][1].result(timeout=5)
    finally:
        request_context.reset_repo_root(tok)
    assert calls[0]["repo_root"] == str(tmp_path)


def test_enhancer_fires_hook_before_its_llm_call(monkeypatch):
    from aiforge_core.llm import client
    from aiforge_core.runtime import parallel_subtasks as pp
    from aiforge_core.runtime.parallel_subtasks import _planning_enhance as pe
    order: list = []
    monkeypatch.delenv("AIFORGE_ENHANCER_DISABLE", raising=False)
    monkeypatch.setattr(pe, "_memory_block", lambda p, r: order.append("recall") or "")
    monkeypatch.setattr(client, "complete",
                        lambda *a, **k: order.append("llm") or "a spec")
    pp._enhance("design a parser module for the billing exports",
                on_context=lambda: order.append("hook"))
    assert order == ["recall", "hook", "llm"]
    order.clear()
    pp._enhance("thanks", on_context=lambda: order.append("hook"))
    assert order == []                               # no LLM call → no prefetch


def test_hook_failure_never_costs_the_spec(monkeypatch):
    from aiforge_core.llm import client
    from aiforge_core.runtime import parallel_subtasks as pp
    from aiforge_core.runtime.parallel_subtasks import _planning_enhance as pe
    monkeypatch.delenv("AIFORGE_ENHANCER_DISABLE", raising=False)
    monkeypatch.setattr(pe, "_memory_block", lambda p, r: "")
    monkeypatch.setattr(client, "complete", lambda *a, **k: "a spec for the parser")

    def bad():
        raise RuntimeError("x")
    assert pp._enhance("design a parser module for the billing exports",
                       on_context=bad) == "a spec for the parser"


def test_concurrent_sessions_do_not_cross(monkeypatch, tmp_path):
    calls: list = []
    monkeypatch.setattr("aiforge_core.memory.unified_query.query",
                        _fake_query(calls, delay=0.05))
    _recall_prefetch.start(_msgs("ask one"), str(tmp_path), 11)
    _recall_prefetch.start(_msgs("ask two"), str(tmp_path), 12)
    out: dict = {}
    ts = [threading.Thread(target=lambda s=s, q=q: out.__setitem__(
        s, chat_agent._memory_recall(str(tmp_path), q, limit=6, session_id=s)))
        for s, q in ((11, "ask one"), (12, "ask two"))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(c["text"] for c in calls) == ["ask one", "ask two"]
    assert out[11] == out[12] != ""
