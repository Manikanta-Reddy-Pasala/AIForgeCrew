"""Layer the strict rules on top of the LLM Verifier verdict.

The LLM emits a JSON ``{verdict, issues, rationale}`` after a single
completion. This function runs the deterministic rules and merges any
strict issues into that verdict. Either side can flip ``verdict`` to
``"reject"``; the strict rules never relax a reject already issued by
the LLM.

KISS contract:
* rules are pure functions ``(plan) -> list[issue_dict]``
* a buggy rule can NEVER crash the orchestrator — exceptions are
  surfaced as a single ``strict_rule_error`` issue
"""
from __future__ import annotations

from .rules import RULES


def apply(plan: dict, base_verdict: dict | None = None) -> dict:
    """Return the merged verdict dict.

    Args:
      plan: parsed plan dict (top-level keys ``child_subtickets`` etc.)
      base_verdict: the LLM verifier's JSON output dict. ``None`` is
        treated as a default ``pass`` — useful for tests + for runs
        where the orchestrator skips the LLM verifier (e.g. trivial
        tickets short-circuited by triage).
    """
    base = dict(base_verdict or {"verdict": "pass", "issues": [], "rationale": ""})
    base.setdefault("issues", [])
    if not isinstance(base["issues"], list):
        base["issues"] = []

    strict_issues: list[dict] = []
    for rule in RULES:
        try:
            strict_issues.extend(rule(plan))
        except Exception as exc:  # rule bugs MUST not crash the loop
            strict_issues.append({
                "kind": "strict_rule_error",
                "message": f"{rule.__name__} raised {exc!r}",
            })

    if strict_issues:
        base["verdict"] = "reject"
        base["issues"] = base["issues"] + strict_issues
        if not base.get("rationale"):
            base["rationale"] = (
                f"strict-mode rejected: {len(strict_issues)} structural issue(s)"
            )
    return base


__all__ = ["apply"]
