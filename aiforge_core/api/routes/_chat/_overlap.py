"""The enhancer, started while the task classifier runs.

A fresh long prompt paid two model calls back to back before the agent spoke:
the classifier (which route) and then the enhancer (the restated request). The
enhancer does not depend on the classifier's answer, so on a server with a
spare slot both run at once and the turn saves one whole call.

Only then. On a one-slot server the second call queues behind the first: the
classifier would sit behind a 512-token rewrite and hit its own 15s timeout.
And when the classifier sends the turn to the build pipeline, the pipeline
writes its own 2048-token spec, so the early rewrite is cancelled — its slot
is freed for the pipeline, and its result is never used.

The classifier's answer and everything it decides are unchanged: this module
only moves WHEN the single-agent path's enhance call starts.
"""
from __future__ import annotations

import concurrent.futures as _cf
import contextvars
import os
import threading

from ._core import _af_log


class EarlyEnhance:
    """One enhance call in flight, its cancel token and the prompt it restates."""

    def __init__(self, prompt: str):
        self.prompt = prompt
        self.cancel = threading.Event()
        self.future: _cf.Future = _cf.Future()


def _enabled() -> bool:
    return os.environ.get("AIFORGE_CHAT_OVERLAP_ENHANCE", "1").strip().lower() \
        not in ("0", "false", "no", "off")


def _classifier_role() -> str:
    return os.environ.get("AIFORGE_TASK_ROUTE_ROLE", "triage").strip() or "triage"


def _classifier_will_run(prompt, history, *, team, quick, single_agent) -> bool:
    """The gate in ``_routing._decide_chat_route``: the classifier runs on a
    fresh, non-quick turn that ``_routing._classify_needed`` says needs it (a
    long prompt, or a short one the build regex fires on) — the SAME predicate,
    not a copy of it. Team turns are left alone — they end in the pipeline or
    the team, never the chat enhance."""
    if team or quick or single_agent:
        return False
    try:
        from aiforge_core.runtime import turn_router
        if turn_router.is_followup(history):
            return False
        from aiforge_core.runtime import chat_router

        from ._routing import _classify_needed
        return _classify_needed(chat_router, prompt or "")
    except Exception:  # noqa: BLE001 — unsure means no overlap
        return False


def _enhance_will_run(prompt) -> bool:
    """Whether the single-agent path would enhance this prompt when it is not
    sent to the pipeline (the only case the early call is for)."""
    try:
        from aiforge_core.runtime.chat_agent._native_prompt import (
            is_plan_execution)
        if is_plan_execution(prompt):
            return False
        from ._routing import _should_skip_enhance
        return not _should_skip_enhance(False, False, False, None, prompt)
    except Exception:  # noqa: BLE001
        return False


def _slots_allow() -> bool:
    try:
        from aiforge_core.llm import slots
        return slots.parallel_ok(_classifier_role(), "enhancer")
    except Exception:  # noqa: BLE001
        return False


def start(pc, _pp) -> "EarlyEnhance | None":
    """Start the chat enhance for ``pc`` now, when the classifier is about to
    run and the server can take both. Stored on ``pc._early_enhance``; returns
    it, or None when the turn keeps the sequential order."""
    pc._early_enhance = None
    if not _enabled():
        return None
    body = getattr(pc, "body", None)
    if not _classifier_will_run(
            pc.prompt, pc.history, team=bool(pc.team),
            quick=bool(getattr(body, "quick", False)),
            single_agent=bool(getattr(body, "single_agent", False))):
        return None
    if not _enhance_will_run(pc.prompt) or not _slots_allow():
        return None
    from aiforge_core.runtime import chat_cancel
    if pc.session_id is not None and chat_cancel.is_cancelled(pc.session_id):
        return None
    early = EarlyEnhance(pc.prompt)
    work = _enhance_call(_pp, pc.prompt, pc.history, pc.cwd, pc.session_id,
                         early.cancel)

    def _run():
        try:
            early.future.set_result(work())
        except BaseException as exc:  # noqa: BLE001 — surfaced by take()
            early.future.set_exception(exc)

    # copy_context: the call is metered and traced as this turn's.
    threading.Thread(target=contextvars.copy_context().run, args=(_run,),
                     name="aiforge-early-enhance", daemon=True).start()
    pc._early_enhance = early
    _af_log.info("chat: enhancer started beside the classifier session=%s",
                 pc.session_id)
    return early


def _enhance_call(_pp, prompt, history, cwd, session_id, cancel):
    """The same call ``_stages._enhance_prompt`` makes, bound to its own cancel
    token instead of the session's (Stop still reaches it: :func:`take` and
    :func:`discard` set that token)."""
    from aiforge_core.runtime.chat_agent import _chat_repo_key
    from aiforge_core.runtime.chat_agent._context import _recall_prefetch

    from ._stages import _chat_enhancer_max_tokens

    def _prefetch():
        _recall_prefetch.start(history, cwd, session_id)

    def _call():
        from aiforge_core.llm import client
        client.set_cancel_event(cancel)
        return _pp._enhance(prompt, history=history, cwd=cwd,
                            repo=_chat_repo_key(cwd), on_context=_prefetch,
                            session_id=None,
                            max_tokens=_chat_enhancer_max_tokens())
    return _call


def claim(pc) -> "EarlyEnhance | None":
    """Hand over the early call (once). None when there is none."""
    early = getattr(pc, "_early_enhance", None)
    pc._early_enhance = None
    return early


def discard(pc) -> None:
    """Cancel an early call nobody will use: the turn went to the pipeline,
    was stopped, or ended. A no-op when there is none or it was claimed."""
    early = claim(pc)
    if early is not None:
        early.cancel.set()


def _wait_budget_s() -> float:
    try:
        from aiforge_core.runtime.parallel_subtasks._planning_enhance import (
            _orchestrator_timeout_s)
        return float(_orchestrator_timeout_s()) + 30.0
    except Exception:  # noqa: BLE001
        return 210.0


def warm_slots() -> None:
    """Probe the chat server's slots in the background at turn start, so the
    first caller that needs the answer (the bundle, the end-of-turn
    prediction) finds it cached instead of waiting on the probe."""
    def _warm():
        try:
            from aiforge_core.llm import slots
            slots.llm_slots()
        except Exception:  # noqa: BLE001 — a warm-up never breaks a turn
            pass
    threading.Thread(target=_warm, name="aiforge-slots-warm",
                     daemon=True).start()


def take(early: EarlyEnhance, session_id) -> str:
    """The early call's spec. Stop, a timeout or any error gives the raw
    prompt, as the sequential enhance does."""
    from aiforge_core.runtime.run_interrupt import STOPPED, wait_future
    try:
        got = wait_future(early.future, _wait_budget_s(), session_id)
    except Exception as exc:  # noqa: BLE001
        _af_log.debug("early enhance failed: %s", exc)
        early.cancel.set()
        return early.prompt
    if got is STOPPED:
        early.cancel.set()
        return early.prompt
    return got or early.prompt


__all__ = ["EarlyEnhance", "claim", "discard", "start", "take", "warm_slots"]
