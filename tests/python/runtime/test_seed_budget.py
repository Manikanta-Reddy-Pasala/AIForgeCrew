"""Fix C1 — the Doer seed must be BUDGETED (it's un-condensable on turn 1).

``text_doer._build_seed`` folds plan + gathered-context + memory + toolchain +
rules + corrective signals into ONE user message. On turn 1 there's no history,
so ``chat_agent._compact_convo`` can't shrink it → a 50K-char context brief
overflows a 32K window. These pin the priority-budgeted seed.
"""
from __future__ import annotations

import importlib

import pytest

from aiforge_core.runtime import text_doer as td


@pytest.fixture(autouse=True)
def _cfg(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("AIFORGE_LOCAL_CTX_WINDOW", raising=False)
    monkeypatch.delenv("AIFORGE_SEED_BUDGET_FRAC", raising=False)
    import aiforge_core.config.runtime_settings as rsmod
    importlib.reload(rsmod)
    yield
    importlib.reload(rsmod)


def _reload_rs():
    import aiforge_core.config.runtime_settings as rsmod
    importlib.reload(rsmod)


def test_seed_budget_truncates_briefs_keeps_plan(monkeypatch):
    monkeypatch.setenv("AIFORGE_LOCAL_CTX_WINDOW", "32768")
    monkeypatch.setenv("AIFORGE_SEED_BUDGET_FRAC", "0.2")
    _reload_rs()
    plan = "STEP 1 do X in file a.py\nSTEP 2 add a test\nSTEP 3 run pytest"
    state = {
        "plan_md": plan,
        "context_brief_md": "CTXHEAD" + ("c" * 50000) + "CTXTAIL",
        "memory_brief_md": "MEMHEAD" + ("m" * 50000) + "MEMTAIL",
        "replan_note": "REPLANMARKER go smaller this attempt",
        "feedback_verdict": "FEEDBACKMARKER the prior tests failed",
    }
    seed = td._build_seed(state)
    budget = td._seed_budget_chars()

    assert len(seed) <= budget                      # within budget
    assert plan in seed                             # plan FULLY present
    assert "REPLANMARKER" in seed                   # corrective signals survive
    assert "FEEDBACKMARKER" in seed
    assert td._SEED_TRUNC_MARK.strip() in seed      # a brief was truncated
    # Bulky brief tails were cut (they didn't survive whole).
    assert "CTXTAIL" not in seed
    assert "MEMTAIL" not in seed
    # Both bulky briefs are still represented (even split, not one starving).
    assert "CTXHEAD" in seed
    assert "MEMHEAD" in seed


def test_seed_budget_frac_env_tunes(monkeypatch):
    monkeypatch.setenv("AIFORGE_LOCAL_CTX_WINDOW", "32768")
    _reload_rs()
    monkeypatch.setenv("AIFORGE_SEED_BUDGET_FRAC", "0.2")
    small = td._seed_budget_chars()
    monkeypatch.setenv("AIFORGE_SEED_BUDGET_FRAC", "0.5")
    assert td._seed_budget_chars() > small
    # C1: on a TINY window (2K) the seed floor SCALES DOWN — the fixed 8000
    # floor + a full-window output reservation used to overflow window×4.
    # It stays positive and never exceeds what the co-budget leaves.
    monkeypatch.setenv("AIFORGE_LOCAL_CTX_WINDOW", "2048")
    _reload_rs()
    monkeypatch.setenv("AIFORGE_SEED_BUDGET_FRAC", "0.1")
    tiny = td._seed_budget_chars()
    assert tiny >= 0
    assert tiny < 8000  # scaled down, no longer the fixed floor


def test_tiny_seed_passes_through_unchanged(monkeypatch):
    monkeypatch.setenv("AIFORGE_LOCAL_CTX_WINDOW", "32768")
    _reload_rs()
    state = {"plan_md": "do the thing", "rules_md": "follow the style guide"}
    seed = td._build_seed(state)
    assert "do the thing" in seed
    assert "follow the style guide" in seed
    assert td._SEED_TRUNC_MARK.strip() not in seed   # nothing truncated
