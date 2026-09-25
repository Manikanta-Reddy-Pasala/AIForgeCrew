"""A short note back into the chat that scheduled a job.

The Jobs page chip and the log line stay. A person who asked from chat
should see the outcome there, in one or two sentences, not a log dump.
"""
from __future__ import annotations

import logging

log = logging.getLogger("aiforge.jobs")

_MAX = 400


def crisp(text: str) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= _MAX:
        return text
    return text[: _MAX - 1] + "…"


def post(job: dict, text: str) -> None:
    """Append ``text`` to the job's chat session, when it has one."""
    sid = job.get("session_id")
    if not sid:
        return
    line = crisp(text)
    if not line:
        return
    try:
        from aiforge_core.runtime import chat_store
        chat_store.add_message(int(sid), "assistant", line)
    except Exception as exc:  # noqa: BLE001 — reporting must not fail the job
        log.warning("jobs.outcome session=%s: %s", sid, exc)
        return
    try:
        from aiforge_core.runtime import hooks
        fired = hooks.fire(
            "Notification",
            {"reason": "finished", "text": line, "job_id": job.get("id")},
            None)
        note = hooks.context_note(fired)
        if note:
            from aiforge_core.runtime import chat_store
            chat_store.add_message(
                int(sid), "user",
                f"[hook Notification — not the user]\n{note}")
    except Exception as exc:  # noqa: BLE001
        log.debug("jobs.outcome notification: %s", exc)
