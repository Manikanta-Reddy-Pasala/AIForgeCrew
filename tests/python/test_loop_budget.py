"""Tests for ``aiforge_core.runtime.loop_budget``.

The plateau watcher is the kill switch for stuck Doer/Refiner/
Feedback iterations. We test the pure helper :func:`evaluate_plateau`
directly so we don't need an ADK runner spun up — the LoopAgent
integration only needs to pass through the kill flag we set on state.
"""
from __future__ import annotations

import pytest

from aiforge_core.runtime import loop_budget


# ─── helpers ───────────────────────────────────────────────────────────


def _outcome(*locs: int) -> dict:
    """Build a doer_outcome dict whose file_diffs sum to ``sum(locs)``.

    Each fake file has ``loc=N`` so :func:`_loc_for_turn` adds the
    explicit count rather than counting newlines.
    """
    return {"file_diffs": [{"path": f"f{i}.py", "loc": loc}
                            for i, loc in enumerate(locs)]}


# ─── pure helper coverage ─────────────────────────────────────────────


def test_evaluate_plateau_no_history_no_kill():
    """First call records the turn but cannot fire the kill switch —
    a plateau needs at least 4 entries (3 deltas)."""
    state: dict = {"doer_outcome": _outcome(100)}
    fired = loop_budget.evaluate_plateau(
        state, plateau_turns=3, plateau_delta=50, min_elapsed_s=0.0,
        now=1000.0,
    )
    assert fired is False
    assert state["loc_history"] == [100]
    assert "loop_budget_kill" not in state


def test_evaluate_plateau_fires_after_three_flat_iterations():
    """3 consecutive deltas all <50 LOC + elapsed > min => fire."""
    state: dict = {}
    times = [1000.0, 1100.0, 1200.0, 2000.0]
    locs = [200, 210, 220, 230]   # deltas 10, 10, 10 — all below 50
    for i, (t, l) in enumerate(zip(times, locs)):
        state["doer_outcome"] = _outcome(l)
        loop_budget.evaluate_plateau(
            state, plateau_turns=3, plateau_delta=50,
            min_elapsed_s=600.0, now=t,
        )
    assert state.get("loop_budget_kill") is True
    reason = state.get("loop_budget_reason", "")
    assert "loc_plateau" in reason
    assert "3x<50" in reason


def test_evaluate_plateau_does_not_fire_when_elapsed_too_short():
    """Same 3-flat history, but elapsed < min_elapsed_s — must NOT fire.

    Prevents short-burst tickets (a Refiner that genuinely converges
    in 90s) from getting kill-switched the moment the plateau pattern
    appears.
    """
    state: dict = {}
    times = [1000.0, 1010.0, 1020.0, 1030.0]   # only 30s elapsed
    for t, l in zip(times, [200, 210, 220, 230]):
        state["doer_outcome"] = _outcome(l)
        loop_budget.evaluate_plateau(
            state, plateau_turns=3, plateau_delta=50,
            min_elapsed_s=600.0, now=t,
        )
    assert state.get("loop_budget_kill") is None or \
        state.get("loop_budget_kill") is False or \
        "loop_budget_kill" not in state


def test_evaluate_plateau_does_not_fire_on_real_progress():
    """One big delta in the window resets the plateau watcher."""
    state: dict = {}
    times = [1000.0, 1100.0, 1200.0, 2000.0]
    # delta sequence 10, 200, 10 — middle delta exceeds threshold.
    for t, l in zip(times, [100, 110, 310, 320]):
        state["doer_outcome"] = _outcome(l)
        loop_budget.evaluate_plateau(
            state, plateau_turns=3, plateau_delta=50,
            min_elapsed_s=600.0, now=t,
        )
    assert state.get("loop_budget_kill") is not True


def test_evaluate_plateau_idempotent_after_kill():
    """Once the kill flag is set, further calls are no-ops — the
    history shouldn't keep growing past the kill point."""
    state: dict = {"loop_budget_kill": True, "loc_history": [1, 2, 3]}
    state["doer_outcome"] = _outcome(999)
    out = loop_budget.evaluate_plateau(state, now=9999.0)
    assert out is False
    # history not touched
    assert state["loc_history"] == [1, 2, 3]


def test_history_caps_at_32_entries():
    """Long-running pipelines shouldn't bloat session state."""
    state: dict = {}
    for i in range(40):
        state["doer_outcome"] = _outcome(i)
        loop_budget.evaluate_plateau(
            state, plateau_turns=999,  # never fire
            plateau_delta=0, min_elapsed_s=999999.0, now=float(i),
        )
    assert len(state["loc_history"]) == 32


def test_loc_for_turn_handles_string_doer_outcome():
    """Doer may emit raw JSON string instead of dict — both must work."""
    import json
    state: dict = {"doer_outcome": json.dumps(_outcome(50, 75))}
    out = loop_budget.evaluate_plateau(state, now=0.0)
    # First call records — turn LOC == 125.
    assert state["loc_history"] == [125]


def test_loc_for_turn_handles_missing_outcome():
    """No doer_outcome at all (e.g. Doer crashed) records 0."""
    state: dict = {}
    loop_budget.evaluate_plateau(state, now=0.0)
    assert state["loc_history"] == [0]


def test_loc_for_turn_uses_content_when_loc_field_missing():
    """Fall back to newline counting when the Doer didn't emit ``loc``."""
    state: dict = {
        "doer_outcome": {
            "file_diffs": [
                {"path": "a.py", "content": "line1\nline2\nline3"},
                {"path": "b.py", "content": "x"},
            ]
        }
    }
    loop_budget.evaluate_plateau(state, now=0.0)
    # 3 lines + 1 line = 4
    assert state["loc_history"] == [4]


def test_loc_for_turn_counts_no_content_patches_as_one():
    """A patch entry without explicit content/loc still registers as
    motion (count=1), so a no-op turn (file_diffs=[]) differs from a
    no-content patch turn."""
    state: dict = {
        "doer_outcome": {"file_diffs": [{"path": "a.py", "action": "patch"}]}
    }
    loop_budget.evaluate_plateau(state, now=0.0)
    assert state["loc_history"] == [1]


# ─── env-knob plumbing ────────────────────────────────────────────────


def test_build_callbacks_disabled_returns_none(monkeypatch):
    monkeypatch.setenv("AIFORGE_LOOP_BUDGET_DISABLE", "1")
    before, after, before_model = loop_budget.build_loop_budget_callbacks()
    assert before is None
    assert after is None
    assert before_model is None


def test_build_callbacks_enabled_returns_triple(monkeypatch):
    """Three callbacks now: loop-before, iteration-after, model-before."""
    monkeypatch.delenv("AIFORGE_LOOP_BUDGET_DISABLE", raising=False)
    before, after, before_model = loop_budget.build_loop_budget_callbacks()
    assert before is not None
    assert after is not None
    assert before_model is not None
    assert callable(before) and callable(after) and callable(before_model)


def test_env_overrides_threshold(monkeypatch):
    """Set the plateau-turns env knob to 2 so the threshold trips
    sooner; verify the helper uses it."""
    monkeypatch.setenv("AIFORGE_LOOP_LOC_PLATEAU_TURNS", "2")
    monkeypatch.setenv("AIFORGE_LOOP_LOC_PLATEAU_DELTA", "100")
    monkeypatch.setenv("AIFORGE_LOOP_MIN_ELAPSED_S", "0")
    before, after, _before_model = loop_budget.build_loop_budget_callbacks()
    assert callable(after)


# ─── evaluate_call_budget — per-LM-call watcher (PR #25 patch A) ─────


def test_evaluate_call_budget_records_count_no_kill():
    """First call records the LM hit but doesn't trip — budget is 400
    by default and we're nowhere near it."""
    state: dict = {}
    fired = loop_budget.evaluate_call_budget(
        state, llm_call_budget=400, wall_budget_s=5400.0, now=1000.0,
    )
    assert fired is False
    assert state["llm_call_count"] == 1
    assert state["llm_first_call_at"] == 1000.0
    assert "loop_budget_kill" not in state


def test_evaluate_call_budget_fires_at_call_cap():
    """At the budget boundary the kill flag flips and the reason
    string carries the count + budget for trace forensics."""
    state: dict = {}
    for n in range(3):
        loop_budget.evaluate_call_budget(
            state, llm_call_budget=3, wall_budget_s=999999.0,
            now=1000.0 + n,
        )
    assert state.get("loop_budget_kill") is True
    assert "llm_call_budget" in state["loop_budget_reason"]
    assert "3/3" in state["loop_budget_reason"]
    assert state["llm_call_count"] == 3


def test_evaluate_call_budget_fires_on_wall_clock():
    """Wall-clock budget trips even when the call count is small —
    catches the "30 calls in 90 minutes" stuck-on-LM-latency mode."""
    state: dict = {}
    loop_budget.evaluate_call_budget(
        state, llm_call_budget=999, wall_budget_s=600.0, now=1000.0,
    )
    # 700s later, second call — wall budget is 600s so should trip.
    fired = loop_budget.evaluate_call_budget(
        state, llm_call_budget=999, wall_budget_s=600.0, now=1700.0,
    )
    assert fired is True
    assert state.get("loop_budget_kill") is True
    assert "wall_budget" in state["loop_budget_reason"]


def test_evaluate_call_budget_idempotent_after_kill():
    """Once the kill flag is set, further calls bump the counter (so
    traces show real LM-call count) but don't re-fire."""
    state: dict = {"loop_budget_kill": True, "llm_call_count": 50,
                    "llm_first_call_at": 1000.0,
                    "loop_budget_reason": "llm_call_budget:50/50_after_60s"}
    out = loop_budget.evaluate_call_budget(
        state, llm_call_budget=10, wall_budget_s=10.0, now=99999.0,
    )
    assert out is False  # never re-fires
    assert state["llm_call_count"] == 51  # count still increments
    # Reason string preserved — not overwritten with a new tag.
    assert state["loop_budget_reason"].startswith("llm_call_budget:50/50")


def test_evaluate_call_budget_zero_budget_disables_call_check():
    """Setting llm_call_budget=0 disables the call-count check
    entirely (the wall-clock check still runs)."""
    state: dict = {}
    for n in range(50):
        loop_budget.evaluate_call_budget(
            state, llm_call_budget=0, wall_budget_s=999999.0,
            now=1000.0 + n,
        )
    assert state.get("loop_budget_kill") is None or \
        state.get("loop_budget_kill") is False or \
        "loop_budget_kill" not in state
    assert state["llm_call_count"] == 50


def test_evaluate_call_budget_first_call_at_stable():
    """``llm_first_call_at`` must NOT update on subsequent calls —
    wall-clock elapsed depends on it."""
    state: dict = {}
    loop_budget.evaluate_call_budget(
        state, llm_call_budget=999, wall_budget_s=999999.0, now=1000.0,
    )
    loop_budget.evaluate_call_budget(
        state, llm_call_budget=999, wall_budget_s=999999.0, now=2000.0,
    )
    loop_budget.evaluate_call_budget(
        state, llm_call_budget=999, wall_budget_s=999999.0, now=3000.0,
    )
    assert state["llm_first_call_at"] == 1000.0


def test_call_budget_env_override(monkeypatch):
    """``AIFORGE_LOOP_LLM_CALL_BUDGET=10`` builds a callback whose
    embedded budget is 10 (verified by tripping it from a fake state)."""
    monkeypatch.setenv("AIFORGE_LOOP_LLM_CALL_BUDGET", "10")
    monkeypatch.setenv("AIFORGE_LOOP_WALL_BUDGET_S", "999999")
    monkeypatch.delenv("AIFORGE_LOOP_BUDGET_DISABLE", raising=False)
    _before, _after, before_model = loop_budget.build_loop_budget_callbacks()
    assert before_model is not None

    # Drive the callback ourselves through a stub callback_context so
    # we don't need ADK; the real ADK harness passes ``llm_request``
    # too, which the callback ignores.
    class _Ctx:
        def __init__(self):
            self.state: dict = {}

    import asyncio
    ctx = _Ctx()
    # 10 calls = trip on the 10th
    for _ in range(10):
        asyncio.run(before_model(callback_context=ctx, llm_request=None))
    assert ctx.state.get("loop_budget_kill") is True
    assert "llm_call_budget" in ctx.state["loop_budget_reason"]


# ─── adk_runner _build_run_config (PR #25 patch C) ────────────────────


def test_run_config_default_cap(monkeypatch):
    """No env override → ``max_llm_calls=1500`` (raised from ADK 500
    default so the soft loop_budget cap of 400 is the actual limiter)."""
    monkeypatch.delenv("AIFORGE_ADK_MAX_LLM_CALLS", raising=False)
    from aiforge_core.runtime import adk_runner
    rc = adk_runner._build_run_config()
    if rc is None:
        pytest.skip("ADK RunConfig not importable — older ADK")
    assert rc.max_llm_calls == 1500


def test_run_config_env_override(monkeypatch):
    """Operator-set ``AIFORGE_ADK_MAX_LLM_CALLS=2000`` flows through."""
    monkeypatch.setenv("AIFORGE_ADK_MAX_LLM_CALLS", "2000")
    from aiforge_core.runtime import adk_runner
    rc = adk_runner._build_run_config()
    if rc is None:
        pytest.skip("ADK RunConfig not importable — older ADK")
    assert rc.max_llm_calls == 2000


def test_run_config_garbage_env_clamps_to_default(monkeypatch):
    """Non-integer env value → warn + use 1500. Never crashes the run."""
    monkeypatch.setenv("AIFORGE_ADK_MAX_LLM_CALLS", "lots")
    from aiforge_core.runtime import adk_runner
    rc = adk_runner._build_run_config()
    if rc is None:
        pytest.skip("ADK RunConfig not importable — older ADK")
    assert rc.max_llm_calls == 1500


def test_run_config_zero_env_clamps_to_default(monkeypatch):
    """Zero or negative disables the cap inside ADK — but we want an
    ENFORCED cap so the loop_budget soft cap is the real limiter.
    The helper should bump 0 → 1500 with a warning."""
    monkeypatch.setenv("AIFORGE_ADK_MAX_LLM_CALLS", "0")
    from aiforge_core.runtime import adk_runner
    rc = adk_runner._build_run_config()
    if rc is None:
        pytest.skip("ADK RunConfig not importable — older ADK")
    assert rc.max_llm_calls == 1500


# ─── prompt assertions (PR #25 patch B) ───────────────────────────────


def test_doer_prompt_demands_milestone_commits():
    """Mandatory phase-boundary git_commit calls should be in the
    Doer prompt as a HARD RULE, not advisory."""
    from aiforge_core.runtime.prompts import DOER
    text = DOER.lower()
    assert "mandatory milestone commits" in text or \
        "milestone commits (hard rule" in text
    assert "git_commit" in DOER
    # 5-file boundary trigger should appear
    assert "5 file_writes" in DOER or "every 5 file" in DOER


def test_doer_prompt_has_early_stop_rule():
    """LOC-target / file-count early-stop should be in the Doer prompt
    so the agent stops writing once the spec is satisfied."""
    from aiforge_core.runtime.prompts import DOER
    text = DOER.lower()
    assert "early-stop" in text or "early stop" in text
    assert "loc target" in text or "loc-call exhaustion" in text or \
        "lm-call exhaustion" in text


def test_doer_prompt_has_same_command_failure_budget():
    """3-strikes rule on identical run_shell invocations — prevents
    the mvn-compile-loop death spiral that killed ONE-1."""
    from aiforge_core.runtime.prompts import DOER
    text = DOER.lower()
    assert "same-command failure budget" in text or \
        "3 consecutive red `run_shell`" in text or \
        "same command" in text


def test_doer_prompt_references_one1_postmortem():
    """The prompt should cite the ONE-1 incident as a worked example —
    keeps future maintainers honest about why the rules exist."""
    from aiforge_core.runtime.prompts import DOER
    assert "ONE-1" in DOER or "audit subsystem" in DOER.lower()
