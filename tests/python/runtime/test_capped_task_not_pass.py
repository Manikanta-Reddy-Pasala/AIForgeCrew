"""Fix 3 — a capped / incomplete Doer run must NOT ship a false ``pass``.

Two holes are closed:
  * ``run_text_doer`` harvests chat_agent's runaway/deadline banner
    (``"(stopped: ..."``) as the outcome; that must flag the run
    ``stopped``/``incomplete`` — not a clean pass. A normal FINAL is not
    flagged.
  * The deterministic quality gate turns an incomplete-stop into a hard
    fail so a model ``pass`` can't flow through.
"""
from __future__ import annotations

from aiforge_core.runtime import chat_agent as ca
from aiforge_core.runtime import quality_gate
from aiforge_core.runtime import text_doer as td


def _fake_events(*events):
    def _gen(*a, **k):
        yield from events
    return _gen


# ── run_text_doer flags the runaway/deadline banner ──
def test_stopped_banner_flags_incomplete(tmp_path, monkeypatch):
    monkeypatch.setattr(ca, "run_chat_agent", _fake_events(
        {"type": "message",
         "text": "(stopped: hit the runaway safety cap — too many tool calls)"},
        {"type": "done"},
    ))
    out = td.run_text_doer({"plan_md": "p"}, str(tmp_path))
    assert out.get("stopped") is True
    assert out.get("incomplete") is True


def test_deadline_banner_flags_incomplete(tmp_path, monkeypatch):
    monkeypatch.setattr(ca, "run_chat_agent", _fake_events(
        {"type": "message", "text": "(stopped: hit the 900s turn deadline)"},
        {"type": "done"},
    ))
    out = td.run_text_doer({"plan_md": "p"}, str(tmp_path))
    assert out.get("stopped") is True


def test_normal_final_not_flagged(tmp_path, monkeypatch):
    # A real edit is part of "normal": a FINAL with ZERO edits is separately
    # (and correctly) flagged incomplete by the no-edit guard.
    monkeypatch.setattr(ca, "run_chat_agent", _fake_events(
        {"type": "tool", "name": "file_write", "result": {"ok": True}},
        {"type": "message", "text": "FINAL done: implemented impl.py, tests green"},
        {"type": "done"},
    ))
    out = td.run_text_doer({"plan_md": "p"}, str(tmp_path))
    assert not out.get("stopped")
    assert not out.get("incomplete")


# ── quality gate: incomplete-stop + model pass → fail ──
def test_gate_incomplete_stop_fails_even_with_none_signals():
    g = quality_gate.evaluate(
        typecheck_ok=None, tests_ok=None, doer_incomplete=True)
    assert g["gate"] == "fail"
    assert quality_gate.gate_verdict("pass", g) == "fail"


def test_gate_not_incomplete_still_passes():
    g = quality_gate.evaluate(
        typecheck_ok=None, tests_ok=None, doer_incomplete=False)
    assert g["gate"] == "pass"
    assert quality_gate.gate_verdict("pass", g) == "pass"


# ── tests_ok is None + declared tests is behind the strict flag ──
def test_declared_tests_none_gated_by_flag(monkeypatch):
    monkeypatch.delenv("AIFORGE_STRICT_TEST_GATE", raising=False)
    g = quality_gate.evaluate(
        typecheck_ok=None, tests_ok=None, tests_declared=True)
    assert g["gate"] == "pass"  # flag off → no false-negative

    monkeypatch.setenv("AIFORGE_STRICT_TEST_GATE", "1")
    g = quality_gate.evaluate(
        typecheck_ok=None, tests_ok=None, tests_declared=True)
    assert g["gate"] == "fail"


# ── feedback callback downgrades pass on incomplete stop ──
def test_feedback_callback_downgrades_on_incomplete():
    import asyncio

    from aiforge_core.agents import feedback as fb

    cb = fb.make_quality_gate_after_callback()

    class _Ctx:
        state = {"feedback_verdict": "pass", "doer_incomplete": True}

    asyncio.run(cb(callback_context=_Ctx()))
    assert _Ctx.state["feedback_verdict"] == "fail"
