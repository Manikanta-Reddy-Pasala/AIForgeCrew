"""Executor context cleansing — give the Doer a clean session.

The Doer / Refiner run in ADK ``chat`` mode, so the graph replays the WHOLE
prior event stream into them every turn — enhancer exploration, the planner's
brainstorming, researcher dumps, verifier critiques. None of that is needed:
the curated hand-off (plan, scope, rules, verifier verdict, context brief)
already reaches the Doer through its prompt's state-templated blocks
(``{plan_md?}`` / ``{context_brief_md?}`` / ``{rules_md?}`` / …). The replayed
prologue is pure redundancy that bloats the prompt for a slow 120B model.

This is the ADK-native realisation of "construct a clean session for the
executor containing only the slice it needs, omitting the planner history":
a ``before_model_callback`` that rewrites ``llm_request.contents`` to the seed
user message + the most recent N contents (the executor's OWN loop work),
dropping the planning prologue in the middle. The plan itself is untouched —
it lives in the templated prompt, not the replayed history.

Safe: the Doer keeps its own recent tool calls + results (it needs those
within an iteration); only the upstream agents' chatter is cut. Composes with
the global ContextFilterPlugin tail-trim (that runs first; this tightens it
further, executor-only).

Env:
  AIFORGE_EXECUTOR_FOCUS=0   disable (fall back to the global tail-trim only)
  AIFORGE_EXECUTOR_TAIL=20   contents to retain after the seed (the own-work tail)
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("aiforge.executor_focus")


def _disabled() -> bool:
    return os.environ.get("AIFORGE_EXECUTOR_FOCUS", "1") in ("0", "false", "no")


def _tail() -> int:
    try:
        return int(os.environ.get("AIFORGE_EXECUTOR_TAIL", "20"))
    except (TypeError, ValueError):
        return 20


def make_executor_focus_callback(role: str = "doer"):
    """Return a ``before_model_callback`` that strips the planning prologue
    from this agent's replayed history. Returns ``None`` (never short-circuits
    the call) — it only edits ``llm_request.contents`` in place."""

    def _cb(*, callback_context=None, llm_request=None, **_kw):  # noqa: ANN001
        if _disabled() or llm_request is None:
            return None
        n = _tail()
        try:
            contents = list(getattr(llm_request, "contents", None) or [])
            # +1 leaves room for the seed; nothing to gain on a short history.
            if n <= 0 or len(contents) <= n + 1:
                return None
            split = len(contents) - n
            try:
                from google.adk.plugins.context_filter_plugin import (
                    _adjust_split_index_to_avoid_orphaned_function_responses as _adj,
                    _is_human_user_content as _ishuman,
                )
                split = _adj(contents, split)
                seed = [c for c in contents[:split] if _ishuman(c)][:1]
            except Exception:  # noqa: BLE001
                seed = contents[:1]
            kept = seed + list(contents[split:])
            if len(kept) < len(contents):
                llm_request.contents = kept
                log.debug("executor_focus[%s]: %d → %d contents",
                          role, len(contents), len(kept))
        except Exception as exc:  # noqa: BLE001 — never break a model call
            log.debug("executor_focus[%s] skipped: %s", role, exc)
        return None

    return _cb


__all__ = ["make_executor_focus_callback"]
