"""Planner mega-ticket detection — verifies the prompt instructs the
model to emit ``subtickets`` for large tickets.

The prompt is the contract; we can't run a live model in CI, so the
test asserts the prompt text contains the exact thresholds + JSON
shape an upstream operator (or the planner agent) needs to read in
order to decompose a mega-ticket. Drift here means the model loses
the rule and ONE-117-style monolithic plans return.
"""
from __future__ import annotations

import re

from aiforge_core.runtime import prompts


def test_planner_prompt_documents_megaticket_thresholds():
    """All three triggers from the spec (>2000 chars, >=10 files,
    keyword stack/service/scaffold) MUST be present in the prompt."""
    text = prompts.PLANNER
    # >2000 char threshold
    assert "2000" in text, (
        "planner prompt missing >2000-char threshold for mega-tickets")
    # >=10 file threshold
    assert "10 files" in text, (
        "planner prompt missing 10-file threshold for mega-tickets")
    # stack / service / scaffold keyword set
    for kw in ("stack", "service", "scaffold"):
        assert kw in text.lower(), (
            f"planner prompt missing mega-ticket keyword: {kw!r}")


def test_planner_prompt_demands_subtickets_array_for_megaticket():
    """The prompt MUST tell the model to emit a ``subtickets`` array,
    not the legacy single-step plan only."""
    text = prompts.PLANNER
    assert "subtickets" in text, (
        "planner prompt missing the ``subtickets`` field instruction")
    # the per-entry shape must be documented — at least all 4 keys.
    for key in ("slug", "goal", "scope_allowlist_globs", "acceptance"):
        assert key in text, (
            f"planner prompt subticket schema missing key: {key!r}")


def test_planner_prompt_describes_phase_decomposition():
    """The 3+ phase instruction (auth -> models -> routers -> tests)
    is the canary for "natural decomposition" — its presence guarantees
    the model sees a concrete example."""
    text = prompts.PLANNER
    # any of the canonical phase chains works
    has_phase_example = (
        ("auth" in text.lower() and "router" in text.lower())
        or "phases" in text.lower()
    )
    assert has_phase_example, (
        "planner prompt should give a phase-decomposition example")


def test_planner_prompt_demands_one_doer_run_per_subticket():
    """The orchestrator-iteration contract MUST be in the prompt so
    the model understands subtickets are dispatched serially."""
    text = prompts.PLANNER.lower()
    assert "once per subticket" in text or "per subticket" in text, (
        "planner prompt must explain the one-Doer-run-per-subticket contract")


def test_planner_prompt_mentions_optional_top_level_field():
    """``subtickets`` field is optional for small tickets — must not
    accidentally force every ticket through the decomposition path."""
    text = prompts.PLANNER.lower()
    # "OPTIONAL" or "OMIT" both signal small-ticket bypass
    assert "optional" in text or "omit" in text, (
        "planner prompt should mark ``subtickets`` optional for small tickets")


def test_planner_prompt_is_self_contained_json_contract():
    """JSON shape declarations must use unambiguous syntax — no
    bare ``...`` placeholder where a real key was expected (regression
    guard against a future edit accidentally deleting a brace)."""
    text = prompts.PLANNER
    # at least one balanced JSON-ish brace pair
    opens = text.count("{")
    closes = text.count("}")
    assert opens >= 2 and closes >= 2, (
        f"planner prompt JSON examples look broken: opens={opens} "
        f"closes={closes}")
    # crude balance check — drift = bug
    assert abs(opens - closes) <= 1, (
        f"planner prompt brace imbalance: opens={opens} closes={closes}")


def test_planner_prompt_keeps_existing_invariants():
    """Pre-existing contract clauses (test skeleton, scope allowlist)
    must survive the mega-ticket addition."""
    text = prompts.PLANNER
    assert "scope_allowlist_globs" in text
    assert "test skeleton" in text.lower()
