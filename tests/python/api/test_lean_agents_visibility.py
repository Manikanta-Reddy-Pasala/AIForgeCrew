"""Lean Agents catalogue is Chat + Team only — no enhancer/ctx wall."""
from __future__ import annotations

from aiforge_core.api.routes import agents as A


def test_lean_visible_roles_hide_dead_flows(monkeypatch):
    monkeypatch.delenv("AIFORGE_FORCE_FULL_PIPELINE", raising=False)
    monkeypatch.setattr(
        A._acfg, "archetypes",
        lambda: [
            "chat", "triage", "planner", "verifier", "doer", "refiner",
            "feedback", "validator", "learner", "enhancer", "architect",
            "researcher", "ctx_repomap", "ctx_conventions", "gap_eval",
            "verify_correctness", "live_verifier",
        ],
    )
    roles = set(A._visible_roles())
    assert {
        "chat", "triage", "planner", "verifier", "doer", "refiner",
        "feedback", "validator", "learner", "live_verifier",
    } <= roles
    for gone in ("enhancer", "architect", "researcher",
                 "ctx_repomap", "ctx_conventions", "gap_eval",
                 "verify_correctness"):
        assert gone not in roles


def test_lean_groups_collapse_to_chat_and_pipeline(monkeypatch):
    monkeypatch.delenv("AIFORGE_FORCE_FULL_PIPELINE", raising=False)
    assert A._role_group("planner") == "pipeline"
    assert A._role_group("verifier") == "pipeline"
    assert A._role_group("chat") == "chat"
