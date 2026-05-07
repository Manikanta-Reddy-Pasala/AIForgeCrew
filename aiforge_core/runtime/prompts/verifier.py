"""Verifier prompt — single-turn JSON plan critic.

Pairs with ``runtime.verifier_strict`` which post-processes the LLM
verdict with deterministic structural rules; the prompt's reject
criteria below are the floor, the strict-mode rules add a ceiling.
"""
from __future__ import annotations

PROMPT = (
    "You are the plan verifier. Critique the plan in state['plan_md']. "
    "Return STRICT JSON only: "
    "{verdict: pass|reject, issues: [...], rationale: <one-line>}. "
    "Reject if any subticket has empty scope_allowlist_globs, a step "
    "targets a missing file/symbol, or no test subticket exists."
)

__all__ = ["PROMPT"]
