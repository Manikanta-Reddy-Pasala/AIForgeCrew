"""Mid-run steering for the sequential team ADK driver (Gap A, team mode).

The simple/plan ReAct loop (``chat_agent.run_chat_agent``) and the
parallel-team subtask loop (``parallel_subtasks``) already drain
``chat_interject`` mid-run. The sequential team driver (``chat_pipeline`` +
``pipeline.build_pipeline``) never did — a steer posted during a team run sat
queued until ``chat_interject.clear()`` silently dropped it at the end.

Why a ``before_model_callback`` and not a session-state write: ADK's
``InMemorySessionService`` hands each running invocation a
``copy.deepcopy``'d ``Session`` at ``run_async()`` start — an external
``append_event()`` against a session object fetched from OUTSIDE that
invocation updates the STORE, not the copy the running graph is actually
reading from, so it would never reach an in-flight Doer/Refiner turn.
``before_model_callback`` runs INSIDE the live invocation (same object the
graph itself holds) and can edit ``llm_request.contents`` directly — the
ADK-native equivalent of the ReAct loop appending a steer turn to ``convo``
before its next model call. Verified empirically against the installed ADK
version before wiring this in (a session-state injection probe showed the
running invocation never observes it; a contents-mutation probe does).

Session identity: pipeline construction (``build_pipeline``) takes no
session_id — it's a plain graph, reused for tickets and chat alike. The
chat session_id is threaded in via ``AIFORGE_CURRENT_SESSION`` (the same env
var ``chat_pipeline._drive`` already sets for the Doer's ``subtask_update``
tool), read fresh on every model call so it always reflects whichever team
run currently holds the process-wide team-run lock (only one runs at a time
— see ``chat_pipeline._RUN_LOCK``).
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("aiforge.chat_steer_callback")


def _current_session_id() -> int | None:
    raw = os.environ.get("AIFORGE_CURRENT_SESSION")
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def make_steer_before_model_callback(role: str = "doer"):
    """Return a ``before_model_callback`` that folds any queued mid-run
    steer message(s) into this agent's next model call. Returns ``None``
    (never short-circuits) — it only appends to ``llm_request.contents``."""

    def _cb(*, callback_context=None, llm_request=None, **_kw):  # noqa: ANN001
        session_id = _current_session_id()
        if session_id is None or llm_request is None:
            return None
        try:
            from aiforge_core.runtime import chat_interject, chat_steer
            items = chat_interject.drain_items(session_id)
            steers = [t for _k, t in items]
            if not items:
                return None
            from google.genai import types as gtypes
            contents = list(getattr(llm_request, "contents", None) or [])
            for _kind, steer in items:
                contents.append(gtypes.Content(
                    role="user", parts=[gtypes.Part(text=
                        chat_steer.steer_directive(steer) if _kind != "reject"
                        else chat_steer.reject_note(steer))]))
            llm_request.contents = contents
            chat_interject.mark_applied(session_id, steers)
            log.info("chat_steer[%s]: folded %d steer(s) session=%s",
                     role, len(steers), session_id)
        except Exception as exc:  # noqa: BLE001 — steering must never break a model call
            log.debug("chat_steer[%s] skipped: %s", role, exc)
        return None

    return _cb


__all__ = ["make_steer_before_model_callback"]
