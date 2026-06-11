"""Regression tests for the round-2 audit fixes.

Covers: C1 content-tail context trim, C2 plan_promote scope promotion,
C3 failure_memory tolerant verdict parse, C5 loop-kill partial verdict,
C11 quality-gate signal writer, C8/C10 single_turn + retry wiring.
"""
from __future__ import annotations

import asyncio

from google.genai import types as gtypes

from aiforge_core.runtime import graph_pipeline as gp


class _FakeCtx:
    def __init__(self, state: dict) -> None:
        self.state = state
        self.route = None


def _run(coro):
    return asyncio.run(coro)


# ── C1: content-tail trim actually fires ───────────────────────────────

def _content(role: str, text: str) -> gtypes.Content:
    return gtypes.Content(role=role, parts=[gtypes.Part.from_text(text=text)])


def test_context_filter_trims_long_single_invocation_run(monkeypatch) -> None:
    """A Workflow run has ONE user message; the old invocation-keep trim
    never fired. The content-tail filter must trim anyway."""
    monkeypatch.setenv("AIFORGE_CONTEXT_MAX_CONTENTS", "20")
    monkeypatch.delenv("AIFORGE_CONDENSER_STRATEGY", raising=False)
    from aiforge_core.runtime.adk_runner import _build_context_plugins
    plugins = _build_context_plugins()
    assert plugins, "plugin must be wired"
    custom = plugins[0]._custom_filter
    assert custom is not None, "content-tail filter must be installed"
    seed = _content("user", "TICKET: do the thing")
    contents = [seed] + [_content("model", f"turn {i}") for i in range(100)]
    out = custom(contents)
    # trimmed to seed + last 20
    assert len(out) == 21, len(out)
    assert out[0] is seed  # seed user message survives
    assert out[-1].parts[0].text == "turn 99"


def test_context_filter_short_run_untouched(monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_CONTEXT_MAX_CONTENTS", "60")
    from aiforge_core.runtime.adk_runner import _build_context_plugins
    custom = _build_context_plugins()[0]._custom_filter
    contents = [_content("user", "seed"), _content("model", "a")]
    assert custom(contents) == contents


# ── C2: plan_promote pulls scope globs out of plan_md ──────────────────

def test_plan_promote_promotes_subticket_globs() -> None:
    plan = (
        '{"plan_md": "do it", "subtickets": ['
        '{"slug": "a", "scope_allowlist_globs": ["src/a/**"]},'
        '{"slug": "b", "scope_allowlist_globs": ["src/b/**", "src/a/**"]}]}'
    )
    state = {"plan_md": plan}
    _run(gp._plan_promote(_FakeCtx(state)))
    assert state["scope_allowlist_globs"] == ["src/a/**", "src/b/**"]


def test_plan_promote_unions_with_seeded_globs() -> None:
    plan = '{"subtickets": [{"scope_allowlist_globs": ["src/x/**"]}]}'
    state = {"plan_md": plan, "scope_allowlist_globs": ["docs/**"]}
    _run(gp._plan_promote(_FakeCtx(state)))
    assert state["scope_allowlist_globs"] == ["docs/**", "src/x/**"]


def test_plan_promote_handles_fenced_and_garbage() -> None:
    state = {"plan_md": '```json\n{"scope_allowlist_globs": ["a/**"]}\n```'}
    _run(gp._plan_promote(_FakeCtx(state)))
    assert state["scope_allowlist_globs"] == ["a/**"]
    state2 = {"plan_md": "not json at all"}
    _run(gp._plan_promote(_FakeCtx(state2)))
    assert "scope_allowlist_globs" not in state2  # soft no-op


# ── C3: failure_memory tolerant verdict parse ──────────────────────────

def test_failure_memory_two_line_pass_is_not_a_failure(monkeypatch) -> None:
    """Real feedback output is 'pass\\n<rationale>' — must NOT record."""
    import json

    from aiforge_core.runtime import failure_memory
    cb = failure_memory.make_failure_memory_after_callback()
    state = {
        "ticket_identifier": "ONE-9",
        "ticket_project": "Repo",
        "feedback_verdict": "pass\nLGTM, tests green.",
        "validator_verdict": json.dumps({"verdict": "approve"}),
    }
    called = {"n": 0}
    monkeypatch.setattr(
        failure_memory, "record_failure",
        lambda *a, **kw: called.__setitem__("n", called["n"] + 1),
    )
    ctx = _FakeCtx(state)
    _run(cb(callback_context=ctx))
    assert called["n"] == 0


# ── C5: loop-budget kill sets a partial verdict ────────────────────────

def test_loop_gate_kill_sets_partial_verdict() -> None:
    state = {
        "feedback_verdict": "fail\nstuck",
        "loop_budget_kill": True,
        "loop_budget_reason": "LOC plateau 3 turns",
    }
    ctx = _FakeCtx(state)
    _run(gp._loop_gate(ctx))
    assert ctx.route == gp.ROUTE_EXIT
    assert state["feedback_verdict"].startswith("partial")
    assert "LOC plateau" in state["feedback_verdict"]


def test_loop_gate_kill_does_not_demote_pass() -> None:
    state = {"feedback_verdict": "pass\nall good", "loop_budget_kill": True}
    _run(gp._loop_gate(_FakeCtx(state)))
    assert state["feedback_verdict"].startswith("pass")


# ── C11: quality-gate signal writer ────────────────────────────────────

class _Tool:
    def __init__(self, name):
        self.name = name


class _ToolCtx:
    def __init__(self, state):
        self.state = state


def test_quality_signal_callback_records_tool_results() -> None:
    from aiforge_core.runtime.quality_gate import make_quality_signal_callback
    cb = make_quality_signal_callback()
    state: dict = {}
    _run(cb(tool=_Tool("run_tests"), args={}, tool_context=_ToolCtx(state),
            tool_response={"ok": False, "language": "python"}))
    _run(cb(tool=_Tool("typecheck"), args={}, tool_context=_ToolCtx(state),
            tool_response={"ok": True}))
    _run(cb(tool=_Tool("editor"), args={}, tool_context=_ToolCtx(state),
            tool_response={"ok": True}))  # unmapped tool → no key
    assert state == {"tests_ok": False, "typecheck_ok": True}


def test_quality_signal_callback_never_alters_response() -> None:
    from aiforge_core.runtime.quality_gate import make_quality_signal_callback
    cb = make_quality_signal_callback()
    out = _run(cb(tool=_Tool("run_tests"), args={},
                  tool_context=_ToolCtx({}), tool_response={"ok": True}))
    assert out is None


# ── C8/C10: single_turn judges + retry on chokepoints ──────────────────

def test_pipeline_modes_and_retry(monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_ESCALATE_DISABLE", "1")
    from aiforge_core.runtime.pipeline import build_pipeline
    p = build_pipeline(skip_researcher=True)
    nodes = {n.name: n for n in p.graph.nodes}
    for judge in ("triage", "validator", "verify_correctness",
                  "verify_scope", "verify_risk"):
        assert nodes[judge].mode == "single_turn", judge
    for chatty in ("enhancer", "planner", "doer", "refiner", "feedback",
                   "learner"):
        assert nodes[chatty].mode == "chat", chatty
    for guarded in ("triage", "enhancer", "planner", "doer", "validator",
                    "verify_scope"):
        assert nodes[guarded].retry_config is not None, guarded
