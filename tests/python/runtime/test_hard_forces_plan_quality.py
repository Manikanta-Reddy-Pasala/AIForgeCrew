"""Hard triage keeps plan critic + polish even on a one-step plan."""
from __future__ import annotations

from types import SimpleNamespace

from aiforge_core.runtime.graph_pipeline import _gates as G


def _promote(plan_md: str, complexity: str):
    ctx = SimpleNamespace(state={
        "plan_md": plan_md,
        "complexity": complexity,
    }, route=None)
    G._plan_promote(ctx)
    return ctx.state


_SMALL_PLAN = (
    '{"goal":"x","subtickets":[],'
    '"scope_allowlist_globs":["mathutil.py"]}'
)


def test_moderate_small_plan_stays_small():
    st = _promote(_SMALL_PLAN, "moderate")
    assert st.get("plan_small") is True


def test_hard_clears_plan_small():
    st = _promote(_SMALL_PLAN, "hard")
    assert st.get("plan_small") is False


def test_complex_synonym_clears_plan_small():
    st = _promote(_SMALL_PLAN, "difficult")
    assert st.get("plan_small") is False
