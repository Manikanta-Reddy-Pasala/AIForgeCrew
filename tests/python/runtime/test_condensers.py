from __future__ import annotations

from aiforge_core.runtime.condensers import condense


def _make_events(n: int) -> list[dict]:
    return [
        {"type": "tool_call", "role": "doer", "text": f"event {i}"}
        for i in range(n)
    ]


def test_noop_is_identity():
    evts = _make_events(50)
    out = condense(evts, "noop")
    assert out == evts
    assert out is not evts  # returns a copy


def test_recent_keeps_last_n():
    evts = _make_events(100)
    out = condense(evts, "recent", keep=20)
    assert len(out) == 20
    assert out[0]["text"] == "event 80"
    assert out[-1]["text"] == "event 99"


def test_recent_keep_zero_returns_empty():
    out = condense(_make_events(10), "recent", keep=0)
    assert out == []


def test_amortized_no_op_below_threshold():
    evts = _make_events(30)
    out = condense(evts, "amortized", threshold=40, keep_tail=20)
    assert out == evts


def test_amortized_compresses_above_threshold():
    evts = _make_events(100)
    out = condense(evts, "amortized", threshold=40, keep_tail=20)
    assert len(out) < len(evts)
    assert out[0]["role"] == "condenser"
    assert "<condensed>" in out[0]["text"]
    assert out[0]["n_compressed"] == 80
    # Tail preserved verbatim
    assert out[-1]["text"] == "event 99"
    assert out[-20]["text"] == "event 80"


def test_amortized_summary_lists_events():
    evts = _make_events(60)
    out = condense(evts, "amortized", threshold=40, keep_tail=20)
    summary = out[0]["text"]
    assert "event 0" in summary
    assert "event 39" in summary


def test_llm_falls_back_to_recent_when_no_summarizer():
    evts = _make_events(50)
    out = condense(evts, "llm", keep=10)
    assert len(out) == 10
    assert out[-1]["text"] == "event 49"


def test_llm_uses_summarizer_when_provided():
    evts = _make_events(50)
    captured = {}
    def _summarize(prompt):
        captured["prompt"] = prompt
        return "- did things\n- finished thing"
    out = condense(evts, "llm", keep=10, summarizer=_summarize)
    assert len(out) == 11  # summary + tail
    assert out[0]["role"] == "condenser"
    assert "<llm_summary>" in out[0]["text"]
    assert "did things" in out[0]["text"]
    assert out[-1]["text"] == "event 49"
    assert "event 0" in captured["prompt"]


def test_llm_falls_back_when_summarizer_raises():
    evts = _make_events(50)
    def _bad(_p):
        raise RuntimeError("LLM down")
    out = condense(evts, "llm", keep=10, summarizer=_bad)
    assert len(out) == 10
    assert all("condenser" != e.get("role") for e in out)


def test_llm_falls_back_when_summarizer_returns_empty():
    evts = _make_events(50)
    out = condense(evts, "llm", keep=10, summarizer=lambda _p: "")
    assert len(out) == 10


def test_unknown_strategy_is_noop():
    evts = _make_events(20)
    out = condense(evts, "weird-thing")
    assert out == evts


# ── Gap #4: PreCompact memory re-injection ──────────────────────────


def test_memory_injector_prepends_block_when_condensation_fires():
    evts = _make_events(100)
    dropped_seen = {}

    def _inject(dropped):
        dropped_seen["n"] = len(dropped)
        return "fact A survived\nfact B survived"

    out = condense(evts, "recent", keep=20, memory_injector=_inject)
    # 20 tail + 1 recovered memory event at the head.
    assert len(out) == 21
    head = out[0]
    assert head.get("role") == "memory"
    assert "fact A survived" in head["text"]
    # injector saw the 80 dropped events.
    assert dropped_seen["n"] == 80
    # original tail preserved at the end.
    assert out[-1]["text"] == "event 99"


def test_memory_injector_not_called_when_no_condensation():
    evts = _make_events(10)
    calls = []
    condense(evts, "recent", keep=50,
             memory_injector=lambda d: calls.append(d) or "x")
    assert calls == []


def test_memory_injector_failure_is_swallowed():
    evts = _make_events(100)

    def _bad(_dropped):
        raise RuntimeError("memory backend down")

    out = condense(evts, "recent", keep=20, memory_injector=_bad)
    # Condensation still happened; no memory event prepended.
    assert len(out) == 20
    assert all(e.get("role") != "memory" for e in out)


def test_memory_injector_empty_result_prepends_nothing():
    evts = _make_events(100)
    out = condense(evts, "recent", keep=20, memory_injector=lambda d: "  ")
    assert len(out) == 20
    assert all(e.get("role") != "memory" for e in out)
