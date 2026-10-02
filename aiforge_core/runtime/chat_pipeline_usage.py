"""Context-window meter for team/pipeline turns.

The simple chat loop emits a ``usage`` event before every model call (see
chat_agent/_turn/_limits.py) and the UI draws "context 20k / 256k". A team turn
runs several stages (and parallel subtasks) that each carry their own prompt,
and used to emit nothing, so the meter vanished the moment the mode switched.

:class:`PipelineUsage` produces the SAME event shape, plus a ``stage`` label,
so the UI works unchanged. The context size is the provider-reported prompt
size of the most recent request of this turn (``call_meter``), falling back to
a chars/4 estimate of what the stages have exchanged so far. Events are
throttled; a stage change always goes through.
"""
from __future__ import annotations

import time

_MIN_INTERVAL_S = 2.0


def usage_event(session_id, stage: str, est_chars: int = 0) -> "dict | None":
    """One ``usage`` event for ``stage``; None when nothing can be said."""
    try:
        from aiforge_core.llm import call_meter
        from aiforge_core.runtime.chat_agent._context._window import (
            _history_fraction,
            _window_source,
            _window_tokens,
        )
        role = stage or None
        calls = call_meter.snapshot(session_id) if session_id is not None else {}
        tokens = int(calls.get("last_prompt_tokens") or 0)
        reported = tokens > 0
        if not reported:
            tokens = max(0, int(est_chars)) // 4
        win = _window_tokens(role)
        if win <= 0:
            return None
        frac = _history_fraction(role)
        return {"type": "usage", "stage": stage or "",
                "context_chars": tokens * 4, "budget_chars": int(win * frac) * 4,
                "context_tokens": tokens, "window_tokens": win,
                "window_source": _window_source(role)[1],
                "compact_at_tokens": int(win * frac),
                "compact_pct": round(frac * 100),
                "pct": min(100, round(tokens * 100 / max(1, win))),
                "tokens_reported": reported,
                "llm_turn": calls.get("turn", 0),
                "llm_session": calls.get("session", 0),
                "llm_per_min": calls.get("per_minute", 0),
                "llm_turn_failed": calls.get("turn_failed", 0),
                "llm_failed_per_min": calls.get("failed_per_minute", 0),
                "llm_turn_tokens_out": calls.get("turn_tokens_out", 0)}
    except Exception:  # noqa: BLE001 — a meter must never break a turn
        return None


class PipelineUsage:
    """Throttled emitter: ``tick(stage, chars)`` returns an event or None."""

    def __init__(self, session_id, base_chars: int = 0,
                 min_interval: float = _MIN_INTERVAL_S):
        self.session_id = session_id
        self.chars = max(0, int(base_chars))
        self.min_interval = min_interval
        self._stage: "str | None" = None
        self._last = 0.0

    def tick(self, stage: str, chars: int = 0, force: bool = False):
        self.chars += max(0, int(chars))
        now = time.monotonic()
        changed = stage != self._stage
        if not (force or changed or now - self._last >= self.min_interval):
            return None
        ev = usage_event(self.session_id, stage, self.chars)
        if ev is not None:
            self._stage, self._last = stage, now
        return ev


def emit_for_adk_event(q, meter: PipelineUsage, event) -> None:
    """After one finished ADK event: put a throttled usage event on ``q``.
    The stage is the event's author (planner/doer/...); the first event of a
    stage always reports, so the bar moves at each stage start and completion."""
    try:
        from .chat_pipeline_events import _event_text
        author = getattr(event, "author", None) or "agent"
        ev = meter.tick(author, len(_event_text(event)))
        if ev is not None:
            q.put(ev)
    except Exception:  # noqa: BLE001
        pass
