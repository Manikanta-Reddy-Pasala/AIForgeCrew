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
user message + the executor's OWN loop work, dropping the planning prologue in
the middle. The plan itself is untouched — it lives in the templated prompt,
not the replayed history.

The Doer's cut is anchored, not sliding. Its prologue ends at its first own
content (ADK presents every other agent's event as a ``user`` content, the
agent's own as ``model``), and its own work is trimmed in steps of
``AIFORGE_CONTEXT_TRIM_STEP`` contents. A window that slid by one content per
call gave every request a different beginning, so the model server could not
reuse its prompt cache and re-read the whole prompt on every Doer step
(measured: 25 s per step instead of 3 s).

Safe: the Doer keeps its own recent tool calls + results (it needs those
within an iteration); only the upstream agents' chatter is cut. Composes with
the global ContextFilterPlugin tail-trim (that runs first; this tightens it
further, executor-only).

Env:
  AIFORGE_EXECUTOR_FOCUS=0        disable (fall back to the global tail-trim only)
  AIFORGE_EXECUTOR_TAIL=20        contents to retain after the seed (the own-work tail)
  AIFORGE_EXECUTOR_PROLOGUE=keep  the Doer keeps the last prologue contents
                                  until its own work pushes them out (the
                                  earlier behaviour); default: drop them
  AIFORGE_CONTEXT_TRIM_STEP=10    contents dropped at a time (1 = every call)
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


def _keep_prologue() -> bool:
    return os.environ.get("AIFORGE_EXECUTOR_PROLOGUE", "drop").strip().lower() \
        == "keep"


def trim_step() -> int:
    """How many contents a history trim drops at a time
    (``AIFORGE_CONTEXT_TRIM_STEP``, default 10; 1 slides on every call)."""
    try:
        return max(1, int(os.environ.get("AIFORGE_CONTEXT_TRIM_STEP", "10")))
    except (TypeError, ValueError):
        return 10


def stepped_split(total: int, keep: int, step: int) -> int:
    """Where a history of ``total`` contents is cut to keep about its last
    ``keep``: the largest multiple of ``step`` that still leaves ``keep``. The
    cut stays where it is while ``step`` more contents arrive, so consecutive
    requests share their beginning. 0 = nothing to cut."""
    if keep <= 0 or total <= keep:
        return 0
    return ((total - keep) // max(1, step)) * max(1, step)


def _own_start(contents: list) -> int:
    """Index of the agent's first own content — the end of its prologue.
    ``len(contents)`` when it has not produced anything yet."""
    for i, c in enumerate(contents):
        if getattr(c, "role", "") == "model":
            return i
    return len(contents)


def make_executor_focus_callback(role: str = "doer"):
    """Return a ``before_model_callback`` that strips the planning prologue
    from this agent's replayed history. Returns ``None`` (never short-circuits
    the call) — it only edits ``llm_request.contents`` in place."""
    # The Refiner judges the Doer's work, which sits in ITS prologue: only the
    # Doer's own prologue is redundant.
    drops_prologue = role == "doer"

    def _cb(*, callback_context=None, llm_request=None, **_kw):  # noqa: ANN001
        if _disabled() or llm_request is None:
            return None
        n = _tail()
        try:
            contents = list(getattr(llm_request, "contents", None) or [])
            if n <= 0:
                return None
            split = stepped_split(len(contents), n, trim_step())
            if drops_prologue and not _keep_prologue():
                split = max(split, _own_start(contents))
            if split <= 1:      # nothing but the seed in front of the cut
                return None
            try:
                from google.adk.plugins.context_filter_plugin import (
                    _adjust_split_index_to_avoid_orphaned_function_responses as _adj,
                    _is_human_user_content as _ishuman,
                )
                if split < len(contents):   # nothing to pair when all is cut
                    split = _adj(contents, split)
                seed = [c for c in contents[:split] if _ishuman(c)][:1]
            except Exception:  # noqa: BLE001
                seed = contents[:1]
            kept = seed + list(contents[split:])
            if kept and len(kept) < len(contents):
                llm_request.contents = kept
                log.debug("executor_focus[%s]: %d → %d contents",
                          role, len(contents), len(kept))
        except Exception as exc:  # noqa: BLE001 — never break a model call
            log.debug("executor_focus[%s] skipped: %s", role, exc)
        return None

    return _cb


__all__ = ["make_executor_focus_callback", "stepped_split", "trim_step"]
