"""Smoke tests for ``aiforge_core.runtime.pipeline.build_pipeline``.

The pipeline factory composes ADK ``LlmAgent`` instances and wires
loop-budget callbacks across three checkpoints (LoopAgent before-agent,
Refiner after-agent, Doer before-model). This file asserts the wiring
contract — not pipeline behaviour, which would need a live ADK run.

Driven by PR #25: the watchdog factory now returns a 3-tuple, the
Doer's ``before_model_callback`` is freshly populated with the
per-LM-call budget watcher, and the LoopAgent's ``before_agent_callback``
short-circuits on the kill flag. All three slots must end up callable
when the watchdog is enabled, and all three must be ``None``-safe when
``AIFORGE_LOOP_BUDGET_DISABLE=1``.
"""
from __future__ import annotations

import pytest


# Skip the whole module if google.adk isn't importable — the pipeline
# factory imports LoopAgent/SequentialAgent at module level.
pytest.importorskip("google.adk")


def _build(monkeypatch, **env):
    """Build the pipeline with a controlled env. ``env`` keys override
    the operator's defaults; missing keys fall through. Always nukes
    AIFORGE_LOOP_BUDGET_DISABLE first so each test starts from a known
    (enabled) state unless it explicitly flips back off."""
    monkeypatch.delenv("AIFORGE_LOOP_BUDGET_DISABLE", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from aiforge_core.runtime import pipeline as _pipeline
    return _pipeline.build_pipeline()


def _find_loop_agent(root):
    """Drill into the SequentialAgent and return the inner LoopAgent."""
    for sub in root.sub_agents:
        if sub.__class__.__name__ == "LoopAgent":
            return sub
    raise AssertionError(f"LoopAgent not found under {root.name}")


def _find_doer(loop_agent):
    for sub in loop_agent.sub_agents:
        if sub.name == "doer":
            return sub
    raise AssertionError("doer LlmAgent not found in loop")


def _find_refiner(loop_agent):
    for sub in loop_agent.sub_agents:
        if sub.name == "refiner":
            return sub
    raise AssertionError("refiner LlmAgent not found in loop")


def test_loop_agent_has_before_agent_callback_when_watchdog_enabled(
    monkeypatch,
):
    root = _build(monkeypatch)
    loop = _find_loop_agent(root)
    # ADK LoopAgent stores the callback as a single callable or a list
    # of callables; in either case it must be truthy.
    assert loop.before_agent_callback is not None


def test_doer_has_before_model_callback_when_watchdog_enabled(monkeypatch):
    """PR #25 patch A — Doer's before_model_callback is the per-LM-call
    watcher that catches single-mega-iteration runaway."""
    root = _build(monkeypatch)
    loop = _find_loop_agent(root)
    doer = _find_doer(loop)
    cbs = doer.before_model_callback
    assert cbs is not None, "Doer is missing the LM-call watcher"
    # The factory uses list-merge to preserve any pre-existing callback
    # the Doer module may have set; accept both shapes.
    if isinstance(cbs, list):
        assert len(cbs) >= 1
        assert all(callable(c) for c in cbs)
    else:
        assert callable(cbs)


def test_refiner_has_after_agent_callback_when_watchdog_enabled(monkeypatch):
    root = _build(monkeypatch)
    loop = _find_loop_agent(root)
    refiner = _find_refiner(loop)
    cbs = refiner.after_agent_callback
    assert cbs is not None


def test_no_callbacks_when_watchdog_disabled(monkeypatch):
    """Operator opts out via env — the wiring must be a clean pass-through."""
    monkeypatch.setenv("AIFORGE_LOOP_BUDGET_DISABLE", "1")
    from aiforge_core.runtime import pipeline as _pipeline
    root = _pipeline.build_pipeline()
    loop = _find_loop_agent(root)
    doer = _find_doer(loop)
    refiner = _find_refiner(loop)
    # LoopAgent.before_agent_callback is None when watchdog is off.
    assert loop.before_agent_callback is None
    # Doer / Refiner before_model / after_agent depend on whether the
    # archetype module set its own. If it didn't, watchdog-off means
    # they stay None. If it did, our wiring code is a no-op (no append).
    # Assert the watchdog-specific callbacks are NOT present.
    for cbs, label in (
        (doer.before_model_callback, "doer.before_model_callback"),
        (refiner.after_agent_callback, "refiner.after_agent_callback"),
    ):
        if cbs is None:
            continue
        # If there's something, it can't be the loop-budget callback —
        # those callables come from build_loop_budget_callbacks which
        # returned (None, None, None) when disabled, so the wiring loop
        # was skipped. Assertion: any preexisting callback is from the
        # archetype module itself, not from our factory.
        flat = cbs if isinstance(cbs, list) else [cbs]
        for c in flat:
            assert "loop_budget" not in repr(c), \
                f"{label} should not have a loop_budget callback"
