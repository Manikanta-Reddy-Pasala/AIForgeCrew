"""Verdict / complexity parsing + Doer-loop budget sizing.

Split out of the former single-file ``graph_pipeline.py`` (grouped by
concern). Pure state-reading logic; depends only on :mod:`._config`.
No behaviour change.
"""
from __future__ import annotations

import json
import os
from typing import Any

from ._config import (
    ITERS_PER_SUBTASK,
    MAX_DOER_ITERS,
    MAX_DOER_ITERS_CAP,
    MAX_DOER_ITERS_COMPLEX,
    MAX_DOER_ITERS_MODERATE,
    _COMPLEX_TOKENS,
    _COMPLEXITY_STRIP,
    _KNOWN_COMPLEXITY,
    _MODERATE_TOKENS,
    _NUMBERED_LINE_RE,
    _TRIVIAL_SYNONYMS,
    _VERDICT_NEGATIVE,
    _VERDICT_POSITIVE,
)
import re


def _plan_subtask_count(state: Any) -> int:
    """How many subtasks/phases the Planner decomposed the ticket into — the
    driver for the DYNAMIC iteration budget. Tries the structured extractor
    (JSON subtickets / phases) first, then falls back to counting numbered
    markdown lines directly (the extractor needs a JSON brace and returns 0 on a
    pure-markdown numbered plan). Soft-fails to 0 (→ tier floor)."""
    plan = state.get("plan_md")
    try:
        from ..subtasks_callback import _extract_subtickets
        subs = _extract_subtickets(plan)
        if isinstance(subs, list) and subs:
            return len(subs)
    except Exception:  # noqa: BLE001 — never let budget sizing break the loop
        pass
    if isinstance(plan, str) and plan:
        try:
            return len(_NUMBERED_LINE_RE.findall(plan))
        except Exception:  # noqa: BLE001
            return 0
    return 0


def _effective_max_iters(state: Any) -> int:
    """The Doer-loop iteration ceiling for THIS ticket — the MAX of the
    complexity tier and a plan-size-scaled budget (dynamic), clamped to
    :data:`MAX_DOER_ITERS_CAP`. Never below the base cap. See
    :data:`MAX_DOER_ITERS`."""
    try:
        c = _read_complexity(state)
    except Exception:  # noqa: BLE001 — a bad verdict must not unbound the loop
        c = "moderate"
    tier = MAX_DOER_ITERS
    if c in _COMPLEX_TOKENS:
        tier = MAX_DOER_ITERS_COMPLEX
    elif c in _MODERATE_TOKENS:
        tier = MAX_DOER_ITERS_MODERATE
    dynamic = _plan_subtask_count(state) * ITERS_PER_SUBTASK
    return min(MAX_DOER_ITERS_CAP, max(MAX_DOER_ITERS, tier, dynamic))


def _normalize_complexity(text: Any) -> str:
    """Lowercase + strip surrounding whitespace/quotes/fences/punctuation.

    Robust to a local model emitting ``"Trivial."`` / ``" simple "`` /
    ``**easy**`` — all normalise to the bare token.
    """
    return str(text).strip().strip(_COMPLEXITY_STRIP).lower()


def _coerce_complexity_token(text: Any) -> str:
    """A bare (non-JSON) model token → a recognised complexity word.

    Anything not in the known vocabulary defaults to ``"moderate"`` so a
    stray sentence never triggers the fast path (fail toward FULL)."""
    norm = _normalize_complexity(text)
    return norm if norm in _KNOWN_COMPLEXITY else "moderate"


def _triage_strict() -> bool:
    """AIFORGE_TRIAGE_STRICT=1 restores exact-"trivial"-only fast-pathing."""
    return str(os.environ.get("AIFORGE_TRIAGE_STRICT", "")).strip().lower() \
        in ("1", "true", "yes", "on")


def _is_trivial(complexity: Any) -> bool:
    """Whether a (normalised) complexity token takes the fast path."""
    token = _normalize_complexity(complexity)
    if _triage_strict():
        return token == "trivial"
    return token in _TRIVIAL_SYNONYMS


def _read_complexity(state: Any) -> str:
    """Pull the triage complexity verdict from state if present.

    Accepts ``state['complexity']`` (pre-seeded) or the triage agent's
    ``triage_verdict`` (a dict, a JSON string possibly wrapped in prose/
    fences, or a bare one-word token). Defaults to ``"moderate"`` (full
    path) when absent or unrecognised — the fast path only fires on a
    trivial-synonym signal (see :data:`_TRIVIAL_SYNONYMS`).
    """
    try:
        c = state.get("complexity")
        if isinstance(c, str) and c.strip():
            return _normalize_complexity(c) or "moderate"
        raw = state.get("triage_verdict")
        if isinstance(raw, dict):
            return _normalize_complexity(
                raw.get("complexity", "moderate")) or "moderate"
        if isinstance(raw, str) and raw.strip():
            text = raw.strip().strip("`")
            if text[:4].lower() == "json":
                text = text[4:]
            obj: Any = None
            try:
                obj = json.loads(text)
            except Exception:
                # prose {json} prose — brace-balanced fallback
                try:
                    from ..rule_capture import _extract_json
                    obj = _extract_json(raw)
                except Exception:
                    obj = None
            if isinstance(obj, dict) and obj.get("complexity") is not None:
                return _normalize_complexity(obj["complexity"]) or "moderate"
            # No JSON at all → treat the raw text as a bare verdict token.
            return _coerce_complexity_token(raw)
    except Exception:
        pass
    return "moderate"


def _parse_verdict(raw: Any) -> str | None:
    """Best-effort extract a verdict token from a dict / JSON / bare str.

    Hardened against a local model wrapping the verdict in prose
    (``I reject this because {"verdict":"reject"}``): after a clean
    ``json.loads`` fails we brace-balance-extract an embedded object
    (same helper ``parallel_stages._coerce_verdict`` uses), then scan for
    a KNOWN verdict word ANYWHERE in the text — not just the first token.
    A genuinely unparseable string returns ``None`` (the documented
    default: callers treat None as neither pass nor fail — ``_feedback_
    passed`` → False, ``_validator_failed`` → False)."""
    try:
        if isinstance(raw, dict):
            v = raw.get("verdict")
            return str(v).lower() if v is not None else None
        if isinstance(raw, str) and raw.strip():
            text = raw.strip().strip("`")
            if text[:4].lower() == "json":
                text = text[4:]
            # 1. clean parse (fenced or bare JSON object).
            try:
                obj = json.loads(text)
                if isinstance(obj, dict) and obj.get("verdict") is not None:
                    return str(obj["verdict"]).lower()
            except Exception:
                pass
            # 2. brace-balanced extraction — survives ``prose {json} prose``.
            try:
                from ..rule_capture import _extract_json
                obj = _extract_json(raw)
                if isinstance(obj, dict) and obj.get("verdict") is not None:
                    return str(obj["verdict"]).lower()
            except Exception:
                pass
            # 3. bare-token scan anywhere. Negatives win over positives so
            #    an ambiguous verdict fails safe (→ replan), never ships.
            low = text.lower()
            for tok in _VERDICT_NEGATIVE:
                if re.search(rf"\b{tok}\b", low):
                    return tok
            for tok in _VERDICT_POSITIVE:
                if re.search(rf"\b{tok}\b", low):
                    return tok
    except Exception:
        pass
    return None


def _gap_sufficient(raw: Any) -> bool:
    """True when the gap-evaluator judged research sufficient.

    Tolerant: a dict with ``sufficient`` wins; a JSON string is parsed;
    anything unparseable defaults to True so a critic formatting slip
    never traps the pipeline in a re-search loop (mirrors
    parallel_stages._coerce_verdict's fail-open stance)."""
    try:
        obj: Any = raw
        if isinstance(raw, str) and raw.strip():
            text = raw.strip().strip("`")
            if text[:4].lower() == "json":
                text = text[4:]
            obj = json.loads(text)
        if isinstance(obj, dict) and "sufficient" in obj:
            return bool(obj["sufficient"])
    except Exception:
        pass
    return True


def _render_gap_brief(raw: Any) -> str:
    """Render the gap-evaluator's missing/queries into a researcher hint."""
    missing: list = []
    queries: list = []
    try:
        obj: Any = raw
        if isinstance(raw, str):
            text = raw.strip().strip("`")
            if text[:4].lower() == "json":
                text = text[4:]
            obj = json.loads(text)
        if isinstance(obj, dict):
            missing = [str(m) for m in (obj.get("missing") or []) if m]
            queries = [str(q) for q in (obj.get("queries") or []) if q]
    except Exception:
        pass
    lines = ["A prior research pass was judged INCOMPLETE. Specifically "
             "locate the following before the Planner runs:"]
    for m in missing:
        lines.append(f"  - MISSING: {m}")
    for q in queries:
        lines.append(f"  - SEARCH: {q}")
    return "\n".join(lines)


def _validator_failed(state: Any) -> bool:
    """True when the Validator asked for changes (the replan trigger)."""
    v = _parse_verdict(state.get("validator_verdict"))
    return v in ("request_changes", "reject", "fail") if v else False


def _feedback_passed(state: Any) -> bool:
    v = _parse_verdict(state.get("feedback_verdict"))
    return v in ("pass", "approve", "pass_with_warnings") if v else False
