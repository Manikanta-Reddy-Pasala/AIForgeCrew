"""Lean Team/Pipeline graph shape — no live LM.

Default includes verifier/refiner (skip on tiny plans). Never includes
enhancer/context fan-out unless FORCE_FULL.
"""
from __future__ import annotations

from aiforge_core.runtime import pipeline

_LEAN_REQUIRED = {
    "triage", "planner", "verifier", "doer", "refiner", "feedback",
    "validator", "learner", "triage_gate", "loop_gate", "validator_gate",
    "verifier_gate", "plan_promote",
}
_LEAN_FORBIDDEN = {
    "enhancer", "researcher",
    "ctx_repomap", "ctx_conventions", "gap_eval", "live_verifier",
}
_FULL_REQUIRED = {
    "triage", "enhancer", "planner", "doer", "refiner", "validator",
    "learner", "verifier", "ctx_repomap", "ctx_conventions",
}


def _names(p):
    return {n.name for n in p.graph.nodes}


def _edges(p):
    return {(e.from_node.name, e.to_node.name, e.route) for e in p.graph.edges}


def test_lean_default_node_set(monkeypatch):
    monkeypatch.delenv("AIFORGE_FORCE_FULL_PIPELINE", raising=False)
    assert pipeline.lean_pipeline_enabled() is True
    names = _names(pipeline.build_pipeline())
    assert _LEAN_REQUIRED <= names
    assert not (names & _LEAN_FORBIDDEN)


def test_lean_default_edges(monkeypatch):
    monkeypatch.delenv("AIFORGE_FORCE_FULL_PIPELINE", raising=False)
    edges = _edges(pipeline.build_pipeline())
    assert ("triage_gate", "doer", "trivial") in edges
    assert ("triage_gate", "planner", "small") in edges
    assert ("triage_gate", "enhancer", "full") not in edges
    assert ("planner", "plan_promote", None) in edges
    assert ("plan_promote", "verifier", None) in edges
    assert ("doer", "refiner", None) in edges
    assert ("refiner", "feedback", None) in edges


def test_force_full_restores_heavy_nodes(monkeypatch):
    monkeypatch.setenv("AIFORGE_FORCE_FULL_PIPELINE", "1")
    assert pipeline.lean_pipeline_enabled() is False
    names = _names(pipeline.build_pipeline(skip_researcher=True))
    assert _FULL_REQUIRED <= names
    edges = _edges(pipeline.build_pipeline(skip_researcher=True))
    assert ("triage_gate", "enhancer", "full") in edges
