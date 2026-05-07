"""Extra plan-validation rules layered on top of the Verifier verdict (option C).

The Verifier LLM emits a JSON ``{verdict, issues, rationale}`` after a
single completion. This module post-processes the LLM's verdict by
running deterministic structural checks against ``plan_md`` so we don't
rely on the model to catch every shape problem.

Failures here flip the verdict to ``reject`` and append issues with a
``kind`` of ``"strict_<rule>"`` so the Planner gets a concrete fix list
on re-plan.

KISS: rules are pure functions that take the parsed plan dict and
return a list of issue dicts. Add a rule = add a function + register
it in :data:`RULES`.
"""
from __future__ import annotations

from typing import Any, Callable

# A rule is a function that returns issues. Empty list = passed.
Rule = Callable[[dict[str, Any]], list[dict[str, str]]]


MAX_SUBTICKETS = 8        # plans bigger than this are usually mis-scoped
MAX_FILES_PER_SUBTICKET = 5  # subtickets touching 5+ files are over-scoped


def _subtickets(plan: dict) -> list[dict]:
    return [s for s in (plan.get("child_subtickets") or []) if isinstance(s, dict)]


def rule_too_many_subtickets(plan: dict) -> list[dict]:
    sts = _subtickets(plan)
    if len(sts) > MAX_SUBTICKETS:
        return [{
            "kind": "strict_too_many_subtickets",
            "message": f"plan has {len(sts)} subtickets (cap {MAX_SUBTICKETS}); "
                       "split into multiple parent tickets",
        }]
    return []


def rule_overscoped_subticket(plan: dict) -> list[dict]:
    issues: list[dict] = []
    for st in _subtickets(plan):
        sid = st.get("id") or st.get("subticket_id") or "(unnamed)"
        files = st.get("files") or st.get("scope_files") or []
        if isinstance(files, list) and len(files) > MAX_FILES_PER_SUBTICKET:
            issues.append({
                "kind": "strict_overscoped_subticket",
                "message": f"subticket {sid!r} declares {len(files)} files "
                           f"(cap {MAX_FILES_PER_SUBTICKET}); split it",
            })
    return issues


def rule_missing_scope_allowlist(plan: dict) -> list[dict]:
    issues: list[dict] = []
    for st in _subtickets(plan):
        sid = st.get("id") or st.get("subticket_id") or "(unnamed)"
        globs = st.get("scope_allowlist_globs")
        if not globs:
            issues.append({
                "kind": "strict_missing_scope_allowlist",
                "message": f"subticket {sid!r} has empty scope_allowlist_globs",
            })
    return issues


def rule_no_test_subticket(plan: dict) -> list[dict]:
    sts = _subtickets(plan)
    if not sts:
        return []
    has_test = any(
        ("test" in (st.get("id") or "").lower())
        or ("test" in (st.get("title") or "").lower())
        or st.get("kind") == "test"
        for st in sts
    )
    if not has_test:
        return [{
            "kind": "strict_no_test_subticket",
            "message": "no test subticket found — every plan must include "
                       "at least one test subticket per acceptance criterion",
        }]
    return []


RULES: tuple[Rule, ...] = (
    rule_too_many_subtickets,
    rule_overscoped_subticket,
    rule_missing_scope_allowlist,
    rule_no_test_subticket,
)


def apply(plan: dict, base_verdict: dict | None = None) -> dict:
    """Layer strict rules on top of the LLM's verdict.

    Args:
      plan: parsed plan dict (top-level keys ``child_subtickets`` etc.)
      base_verdict: the Verifier's JSON output dict (with ``verdict`` /
        ``issues`` / ``rationale``). When ``None`` we treat it as ``pass``.

    Returns:
      A merged verdict dict. ``verdict`` becomes ``reject`` if any
      strict rule fires; ``issues`` accumulates strict issues alongside
      the LLM's; ``rationale`` is preserved verbatim (or filled with a
      default if missing).
    """
    base = dict(base_verdict or {"verdict": "pass", "issues": [], "rationale": ""})
    base.setdefault("issues", [])
    if not isinstance(base["issues"], list):
        base["issues"] = []

    strict_issues: list[dict] = []
    for rule in RULES:
        try:
            strict_issues.extend(rule(plan))
        except Exception as exc:  # rule bugs must not crash the orchestrator
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


__all__ = ["RULES", "apply",
           "MAX_SUBTICKETS", "MAX_FILES_PER_SUBTICKET",
           "rule_too_many_subtickets", "rule_overscoped_subticket",
           "rule_missing_scope_allowlist", "rule_no_test_subticket"]
