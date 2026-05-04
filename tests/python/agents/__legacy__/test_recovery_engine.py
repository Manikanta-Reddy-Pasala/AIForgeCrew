"""Tests for the recovery engine — closes the gap between detector
matches and orchestrator actions."""
from __future__ import annotations

from aiforge_core.orchestrator import failure_taxonomy as ft
from aiforge_core.orchestrator import circuit_breakers as cb_mod
from aiforge_core.orchestrator.recovery import Action
from aiforge_core.orchestrator.recovery_engine import RecoveryEngine


def test_handle_maps_f001_to_block_and_retry():
    eng = RecoveryEngine(ticket_id="T1")
    match = ft.record("F-001", evidence="com.foo.NotReal")
    decision = eng.handle(match, stage="doer")
    assert decision.action is Action.BLOCK_AND_RETRY
    assert decision.kwargs["evidence"] == "com.foo.NotReal"
    assert decision.mode_id == "F-001"
    assert not decision.halt


def test_handle_maps_f006_to_split_ticket():
    eng = RecoveryEngine(ticket_id="T1")
    match = ft.record("F-006", evidence="steps=15 > max=12")
    decision = eng.handle(match, stage="planner")
    assert decision.action is Action.SPLIT_TICKET


def test_handle_maps_f009_to_replan_smaller_with_shrink_kw():
    eng = RecoveryEngine(ticket_id="T1")
    match = ft.record("F-009", evidence="used=8000 expected=2000")
    decision = eng.handle(match, stage="doer")
    assert decision.action is Action.REPLAN_SMALLER
    assert decision.kwargs.get("shrink") is True
    assert decision.kwargs["unresolved_refs"][0]["mode_id"] == "F-009"


def test_repeat_escalates_after_threshold_and_trips_breaker():
    breakers = cb_mod.CircuitBreakers()
    eng = RecoveryEngine(breakers=breakers, ticket_id="T1")
    match = ft.record("F-001", evidence="x")
    eng.handle(match)  # 1
    eng.handle(match)  # 2
    decision = eng.handle(match)  # 3 → escalate
    assert decision.action is Action.ESCALATE_HUMAN
    assert decision.halt is True
    assert breakers.tripped
    assert "recovery_escalate_f-001" in breakers.state.reason


def test_loop_check_trips_after_three_identical_outputs():
    eng = RecoveryEngine(ticket_id="T1")
    out = "diff that never changes"
    assert eng.loop_check(key="doer:step1", output=out) is None
    assert eng.loop_check(key="doer:step1", output=out) is None
    decision = eng.loop_check(key="doer:step1", output=out)
    assert decision is not None
    assert decision.mode_id == "F-004"


def test_loop_check_keys_are_independent():
    eng = RecoveryEngine(ticket_id="T1")
    out = "x"
    eng.loop_check(key="step1", output=out)
    eng.loop_check(key="step1", output=out)
    # Different key — own buffer
    decision = eng.loop_check(key="step2", output=out)
    assert decision is None


def test_plan_depth_check_emits_split_decision():
    eng = RecoveryEngine(ticket_id="T1")
    deep_plan = {"steps": [{"id": i} for i in range(20)]}
    decision = eng.plan_depth_check(deep_plan)
    assert decision is not None
    assert decision.action is Action.SPLIT_TICKET


def test_plan_depth_check_no_op_for_shallow_plan():
    eng = RecoveryEngine(ticket_id="T1")
    decision = eng.plan_depth_check({"steps": [{"id": 1}]})
    assert decision is None


def test_history_records_all_decisions():
    eng = RecoveryEngine(ticket_id="T1")
    eng.handle(ft.record("F-001", evidence="a"))
    eng.handle(ft.record("F-006", evidence="b"))
    assert len(eng.history) == 2
    assert {d.mode_id for d in eng.history} == {"F-001", "F-006"}
