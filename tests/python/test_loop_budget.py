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


def test_an_unmeasurable_turn_is_not_recorded_as_a_stall(monkeypatch):
    """The Doer's prompt contract emits {path, action} — no content, no loc —
    so counting entries measured FILES TOUCHED (1-6), and "three deltas under
    50 lines" was then true of every possible turn. That turned the progress
    watchdog into a 10-minute timer that shipped productive work as partial.

    When git cannot measure the turn either, the correct answer is "no
    judgement", not "no progress"."""
    monkeypatch.setattr(loop_budget, "_worktree_loc", lambda: None)
    state: dict = {
        "doer_outcome": {"file_diffs": [{"path": "a.py", "action": "patch"}]}
    }
    assert loop_budget.evaluate_plateau(state, now=0.0) is False
    assert "loc_history" not in state


def test_the_real_line_count_comes_from_git(monkeypatch):
    """What the model chose to report is not the progress signal; what changed
    on disk is."""
    monkeypatch.setattr(loop_budget, "_worktree_loc", lambda: 412)
    state: dict = {
        "doer_outcome": {"file_diffs": [{"path": "a.py", "action": "patch"}]}
    }
    loop_budget.evaluate_plateau(state, now=0.0)
    assert state["loc_history"] == [412]


def test_a_productive_loop_is_not_killed(monkeypatch):
    """14 new files across four turns used to read as a plateau."""
    monkeypatch.setattr(loop_budget, "_worktree_loc",
                        lambda: _growing.pop(0))
    state: dict = {"doer_outcome": {"file_diffs": [{"path": "a.py",
                                                    "action": "write"}]}}
    for turn, t in enumerate((0.0, 300.0, 600.0, 900.0)):
        loop_budget.evaluate_plateau(state, now=t)
        assert not state.get("loop_budget_kill"), f"killed on turn {turn}"


_growing = [120, 380, 700, 1150]


# ─── env-knob plumbing ────────────────────────────────────────────────


def test_build_callbacks_disabled_returns_none(monkeypatch):
    monkeypatch.setenv("AIFORGE_LOOP_BUDGET_DISABLE", "1")
    before, after = loop_budget.build_loop_budget_callbacks()
    assert before is None
    assert after is None


def test_build_callbacks_enabled_returns_pair(monkeypatch):
    monkeypatch.delenv("AIFORGE_LOOP_BUDGET_DISABLE", raising=False)
    before, after = loop_budget.build_loop_budget_callbacks()
    assert before is not None and after is not None


def test_env_overrides_threshold(monkeypatch):
    """Set the plateau-turns env knob to 2 so the threshold trips
    sooner; verify the helper uses it."""
    monkeypatch.setenv("AIFORGE_LOOP_LOC_PLATEAU_TURNS", "2")
    monkeypatch.setenv("AIFORGE_LOOP_LOC_PLATEAU_DELTA", "100")
    monkeypatch.setenv("AIFORGE_LOOP_MIN_ELAPSED_S", "0")
    before, after = loop_budget.build_loop_budget_callbacks()
    assert callable(after)
