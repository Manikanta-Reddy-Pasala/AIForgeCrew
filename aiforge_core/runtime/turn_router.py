"""Lightweight per-turn complexity router for team/pipeline chat.

Problem: once a team (pipeline) chat session has produced output, EVERY
follow-up message re-runs the whole heavy graph — enhancer → planner →
context fan-out → verifier → Doer loop in an isolated worktree → validator —
which on a slow local 120B model costs minutes. Most follow-ups ("rename that
var", "add a test", "fix the import") are small and don't need any of that.

This router runs ONE cheap classify (the triage-tier model) on a follow-up
turn and, when the change is small, lets the caller downgrade the turn to the
fast single-agent path (``run_chat_agent``) instead of the full pipeline. The
first turn of a team session is always respected as the user's explicit choice
— only follow-ups are eligible for auto-downgrade.

Safety: any failure (import, LLM, timeout, ambiguous answer) returns
``"complex"`` / ``False`` so we NEVER silently downgrade a genuine build.

Env:
  AIFORGE_TEAM_AUTO_ROUTE=0     disable; every team turn runs the full pipeline
  AIFORGE_TEAM_ROUTE_ROLE=triage  model role used for the classify
  AIFORGE_TEAM_ROUTE_TIMEOUT_S=20 per-call budget
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("aiforge.turn_router")

_SYS = (
    "You are a routing classifier for a coding agent. Decide whether the "
    "user's LATEST message needs the FULL multi-agent pipeline (planning + "
    "architecture + verification — for a new feature, a multi-file change, a "
    "redesign, or anything you must decompose) or just a SIMPLE direct edit "
    "(a small, localized change: edit/rename/fix in one or a few spots, a "
    "quick question, a tweak to what was just produced). "
    "Most follow-ups are SIMPLE. Answer with ONE word: SIMPLE or COMPLEX."
)


def _disabled() -> bool:
    return os.environ.get("AIFORGE_TEAM_AUTO_ROUTE", "1") in ("0", "false", "no")


def _int_env(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


def is_followup(history) -> bool:
    """True when this session already produced an assistant turn — i.e. the
    pipeline (or agent) has run at least once and this is a follow-up."""
    try:
        return any((m or {}).get("role") == "assistant" for m in (history or []))
    except Exception:  # noqa: BLE001
        return False


def _recent_context(history, n: int = 4) -> str:
    """Compact tail of the conversation so a context-dependent follow-up
    ("now also handle nulls") is judged against what came before."""
    out: list[str] = []
    for m in (history or [])[-n:]:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "")
        txt = (m.get("content") or "").strip().replace("\n", " ")
        if txt:
            out.append(f"{role}: {txt[:300]}")
    return "\n".join(out)


def classify(prompt: str, history=None, _cwd: str | None = None) -> str:
    """Return ``"simple"`` or ``"complex"`` for ``prompt``. Deterministic
    trivial/greeting → simple; otherwise one cheap LLM call. Returns
    ``"complex"`` on any failure (safe default — never downgrade silently)."""
    p = (prompt or "").strip()
    if not p:
        return "simple"
    # Deterministic short-circuit for greetings / acks / tiny fragments.
    try:
        from aiforge_core.runtime.parallel_subtasks import _is_trivial_prompt
        if _is_trivial_prompt(p):
            return "simple"
    except Exception:  # noqa: BLE001
        pass
    try:
        from aiforge_core.llm import client as _llm
    except Exception as exc:  # noqa: BLE001
        log.debug("turn_router import failed: %s", exc)
        return "complex"
    role = os.environ.get("AIFORGE_TEAM_ROUTE_ROLE", "triage").strip() or "triage"
    ctx = _recent_context(history)
    user = (f"Recent conversation:\n{ctx}\n\n" if ctx else "") + \
        f"Latest message:\n{p}\n\nSIMPLE or COMPLEX?"
    try:
        raw = _llm.complete(
            role, [{"role": "system", "content": _SYS},
                   {"role": "user", "content": user}],
            max_tokens=8, temperature=0.0,
            timeout_s=_int_env("AIFORGE_TEAM_ROUTE_TIMEOUT_S", 20),
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("turn_router classify failed: %s", exc)
        return "complex"
    low = (raw or "").strip().lower()
    # Word-presence wins; "complex" checked first so "not simple, complex"
    # never reads as simple.
    if "complex" in low:
        return "complex"
    if "simple" in low:
        return "simple"
    return "complex"


def should_downgrade_team(prompt: str, history=None, cwd: str | None = None) -> bool:
    """True when a TEAM turn should be handled by the fast single-agent path
    instead of the full pipeline: enabled, it's a follow-up (not the first
    turn), and the change classifies as simple."""
    if _disabled():
        return False
    if not is_followup(history):
        return False
    return classify(prompt, history, cwd) == "simple"


__all__ = ["is_followup", "classify", "should_downgrade_team"]
