"""Validator — basic post-condition check on Doer's output.

Distinct from Verifier (PreFlect critic on the plan). Validator
runs AFTER Doer and answers: did the diff change what the step
expected to change? Is it free of detector hits?

P1 minimal:
    - decision = "approve" if doer.problems is empty AND udiff non-empty
    - else "block" with reason

P2 will run lint/test in sandbox and feed exit codes here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aiforge_core.agents.base import BaseArchetype
from aiforge_core.agents.registry import register


@register("validator")
@dataclass
class Validator(BaseArchetype):
    name: str = "validator"

    def run(self, *, ctx: dict[str, Any]) -> dict[str, Any]:
        doer_out = ctx.get("doer_outcome", {}) or {}
        problems = doer_out.get("problems") or []
        udiff = doer_out.get("udiff") or ""
        skipped = doer_out.get("skipped", False)

        if skipped:
            return {
                "artifact_type": "validation",
                "decision": "skip",
                "reason": doer_out.get("reason", "doer_skipped"),
                "checks": {},
            }

        checks = {
            "udiff_non_empty":      bool(udiff.strip()),
            "no_hallucinated_imports": not any(p.get("mode") == "F-001" for p in problems),
            "no_diff_hash_mismatch":   not any(p.get("mode") == "F-003" for p in problems),
            "no_hallucinated_symbols": not any(p.get("mode") == "F-002" for p in problems),
        }
        all_pass = all(checks.values())

        return {
            "artifact_type": "validation",
            "decision": "approve" if all_pass else "block",
            "checks": checks,
            "problems": problems,
            "reason": ""
                if all_pass
                else "; ".join(k for k, v in checks.items() if not v),
        }
