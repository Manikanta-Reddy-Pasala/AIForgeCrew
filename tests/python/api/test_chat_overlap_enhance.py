"""The enhancer runs beside the task classifier only when the model server has
a spare slot, and an escalation to the pipeline cancels it.

Overlap is proven with a barrier both fake calls must reach together, never
with wall-clock totals.
"""
from __future__ import annotations

import threading
import time
import types

import pytest

from aiforge_core.api.routes._chat import _overlap, _producer
from aiforge_core.llm import slots
from aiforge_core.runtime import parallel_subtasks

LONG = ("Please restructure the billing module so invoices and refunds share "
        "one ledger writer. " * 8).strip()


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("AIFORGE_CHAT_OVERLAP_ENHANCE", raising=False)
    from aiforge_core.config import _filecache
    _filecache.clear()
    slots.reset()
    yield
    slots.reset()


def _pc(tmp_path, prompt=LONG):
    return types.SimpleNamespace(
        _cmd_help_text="", body=types.SimpleNamespace(quick=False,
                                                      single_agent=False),
        history=[], cwd=str(tmp_path), role="chat", session_id=None,
        _resume_brief=None, _cmd_expanded=None, prompt=prompt,
        _turn_t0=time.time(), team=False, _auto_downgraded=False,
        _parallel_team=False, _path={}, agent_mode="simple", _turn_mode="simple",
        run=None)


class _Rec:
    """Fake enhancer + classifier that share a barrier and record order."""

    def __init__(self, overlap: bool, block_enhance: bool = False):
        self.barrier = threading.Barrier(2) if overlap else None
        self.block_enhance = block_enhance
        self.order: list[str] = []
        self.enhance_calls = 0
        self.enhance_cancelled = threading.Event()
        self.enhance_returned = threading.Event()

    def enhance(self, prompt, **kw):
        self.enhance_calls += 1
        self.order.append("enhance-start")
        if self.barrier is not None:
            self.barrier.wait(timeout=5)
        if self.block_enhance:
            from aiforge_core.llm.client._http import _CANCEL
            ev = _CANCEL.get()
            assert ev is not None, "the early enhance must be cancellable"
            if ev.wait(5):
                self.enhance_cancelled.set()
        self.order.append("enhance-end")
        self.enhance_returned.set()
        return "SPEC: " + prompt[:20]

    def decide(self, route_pipeline):
        def _decide(*_a, **_k):
            self.order.append("classify-start")
            if self.barrier is not None:
                self.barrier.wait(timeout=5)   # both calls in flight at once
            self.order.append("classify-end")
            return types.SimpleNamespace(
                doc_task=False, is_build_task=route_pipeline,
                build_escalate=route_pipeline, route_pipeline=route_pipeline,
                notice=None)
        return _decide


def _wire(monkeypatch, rec: _Rec, route_pipeline: bool, seen: dict):
    def _none(*_a, **_k):
        return iter(())

    for name in ("_early_route_events", "_prelude_notices",
                 "_note_staleness_notice", "_rule_capture_pass",
                 "_post_run_events"):
        monkeypatch.setattr(_producer, name, _none)
    monkeypatch.setattr(_producer, "_warm_repo_map", lambda _cwd: None)
    monkeypatch.setattr(_producer, "_decide_chat_route", rec.decide(route_pipeline))

    def _dispatch(_rd, *_a):
        rctx = _a[-1]
        if _rd.route_pipeline:
            rctx["done"] = True
            yield {"type": "done"}
    monkeypatch.setattr(_producer, "_dispatch_agent_route", _dispatch)
    monkeypatch.setattr(_producer, "_enhance_prompt",
                        lambda _pp, p, *a, **k: rec.enhance(p))
    monkeypatch.setattr(parallel_subtasks, "_enhance", rec.enhance)
    monkeypatch.setattr(_producer, "_fold_enriched_history",
                        lambda h, enriched, *a: seen.setdefault(
                            "enriched", enriched) and [])
    monkeypatch.setattr(_producer, "_commit_simple_baseline",
                        lambda _cwd: ("", True))

    def _agent(*_a, **_k):
        yield {"type": "done"}
    monkeypatch.setattr(_producer, "_single_agent_events", _agent)


def test_spare_slot_runs_enhancer_beside_classifier(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_LLM_PARALLEL", "2")
    rec, seen = _Rec(overlap=True), {}
    _wire(monkeypatch, rec, route_pipeline=False, seen=seen)
    pc = _pc(tmp_path)
    list(_producer._events(pc))      # a barrier timeout would raise here
    assert rec.enhance_calls == 1    # the early result is used, not redone
    assert seen["enriched"].startswith("SPEC: ")
    assert rec.order.index("enhance-start") < rec.order.index("classify-end")
    assert getattr(pc, "_early_enhance", None) is None


def test_one_slot_keeps_the_sequential_order(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_LLM_PARALLEL", "1")
    rec, seen = _Rec(overlap=False), {}
    _wire(monkeypatch, rec, route_pipeline=False, seen=seen)
    list(_producer._events(_pc(tmp_path)))
    assert rec.enhance_calls == 1
    assert rec.order == ["classify-start", "classify-end",
                         "enhance-start", "enhance-end"]


def test_unknown_slots_keep_the_sequential_order(monkeypatch, tmp_path):
    # AIFORGE_LLM_PARALLEL unset: the probe finds nothing listening -> 1.
    monkeypatch.delenv("AIFORGE_LLM_PARALLEL", raising=False)
    rec, seen = _Rec(overlap=False), {}
    _wire(monkeypatch, rec, route_pipeline=False, seen=seen)
    list(_producer._events(_pc(tmp_path)))
    assert rec.order[0] == "classify-start"


def test_escalation_cancels_the_early_enhance(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_LLM_PARALLEL", "2")
    rec, seen = _Rec(overlap=True, block_enhance=True), {}
    _wire(monkeypatch, rec, route_pipeline=True, seen=seen)
    list(_producer._events(_pc(tmp_path)))
    assert rec.enhance_returned.wait(5)
    assert rec.enhance_cancelled.is_set()    # cancelled, not waited out
    assert "enriched" not in seen            # and its result never used


def test_short_prompt_never_starts_early(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_LLM_PARALLEL", "4")
    rec = _Rec(overlap=False)
    monkeypatch.setattr(parallel_subtasks, "_enhance", rec.enhance)
    pc = _pc(tmp_path, prompt="fix the import")
    assert _overlap.start(pc, parallel_subtasks) is None
    assert rec.enhance_calls == 0


def test_stop_while_waiting_returns_the_raw_prompt(monkeypatch):
    early = _overlap.EarlyEnhance("raw ask")
    from aiforge_core.runtime import run_interrupt
    monkeypatch.setattr(run_interrupt, "reason", lambda _sid: "stop")
    assert _overlap.take(early, 7) == "raw ask"
    assert early.cancel.is_set()
