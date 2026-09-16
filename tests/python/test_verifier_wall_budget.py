"""The single-call verifier must not be starved relative to the critics it
replaces.

max_wall_s is the ADK node's HARD timeout, and a hard node timeout aborts the
whole team run. The verifier was given 60s — the smallest budget of any judge —
while doing, in one call, the work the three axis critics each get 300s for.
"""
from __future__ import annotations

from aiforge_core.agents.loader import load_agents


def test_the_verifier_gets_at_least_what_each_critic_it_combines_gets():
    c = load_agents()
    verifier = c["verifier"].contract.max_wall_s
    for critic in ("verify_correctness", "verify_scope", "verify_risk"):
        assert verifier >= c[critic].contract.max_wall_s, (
            f"verifier ({verifier}s) is starved against {critic} "
            f"({c[critic].contract.max_wall_s}s)")


def test_the_verifier_budget_is_applied_as_the_node_timeout():
    """Guard the premise: if this stops being the node timeout, the budget
    above stops mattering and this test should be revisited."""
    import inspect

    from aiforge_core.agents import _base
    src = inspect.getsource(_base.build_llm_agent)
    assert '"timeout": c.contract.max_wall_s' in src
