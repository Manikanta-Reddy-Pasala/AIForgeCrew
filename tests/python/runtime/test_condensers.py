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


def test_llm_falls_back_to_recent():
    evts = _make_events(50)
    out = condense(evts, "llm", keep=10)
    assert len(out) == 10
    assert out[-1]["text"] == "event 49"


def test_unknown_strategy_is_noop():
    evts = _make_events(20)
    out = condense(evts, "weird-thing")
    assert out == evts
