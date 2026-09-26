"""A message typed while background work runs in this chat.

A background watch or a scheduled agent can own a chat between user
turns. A typed message then either stops that work, steers it, or starts
a normal turn. It is never dropped: when the run cannot take it in, the
caller runs it as a normal turn.
"""
from __future__ import annotations

import json
import re

from aiforge_core.api.routes._sse import sse_response

# A question or an explicit new topic is for the chat, not the run. The
# scheduled agent's reply goes to the job outcome, so folding a question
# into it leaves the user with no answer.
_QUESTION_RE = re.compile(
    r"^\s*(?:how|what|why|when|where|which|who|whose|is|are|was|were|does|"
    r"did|do\s+(?:i|we|you)|can\s+(?:i|we)|could\s+(?:i|we)|should|explain|"
    r"tell\s+me|show\s+me)\b",
    re.IGNORECASE,
)
# "can you also log the date?" is a polite instruction, not a question.
_POLITE_ASK_RE = re.compile(
    r"^\s*(?:please\s+)?(?:can|could|would|will)\s+you\b", re.IGNORECASE)
_NEW_TOPIC_RE = re.compile(
    r"^\s*(?:new\s+(?:task|question|request)|separately|unrelated|"
    r"different\s+(?:task|question)|also,?\s+(?:a\s+)?(?:separate|unrelated))\b",
    re.IGNORECASE,
)


def _cut_background_watches(session_id: int, text: str) -> None:
    """An explicit stop ("stop", "stop watching", "cancel it") ends the
    background watches in this chat.

    Any other message leaves them running. Stop still ends them on its own."""
    from aiforge_core.runtime.run_interrupt import text_cuts_running_work
    if not text_cuts_running_work(text):
        return
    try:
        from aiforge_core.runtime import bg_work
        bg_work.stop_session(session_id)
    except Exception:  # noqa: BLE001
        pass


def _is_new_request(text: str) -> bool:
    """True when the message asks something of the chat, not the run."""
    t = (text or "").strip()
    if not t:
        return False
    if _NEW_TOPIC_RE.match(t) or _QUESTION_RE.match(t):
        return True
    return t.endswith("?") and not _POLITE_ASK_RE.match(t)


def _has_new_attachment(session_id: int) -> bool:
    """A file was uploaded after the last message, so it belongs to this one.

    Both timestamps are the store's UTC ISO strings, so they compare as text."""
    from aiforge_core.runtime import chat_store
    try:
        media = chat_store.list_media(session_id)
        if not media:
            return False
        rows = chat_store.get_messages(session_id)
    except Exception:  # noqa: BLE001
        return False
    newest_media = max(str(m.get("created_at") or "") for m in media)
    last_msg = str(rows[-1].get("created_at") or "") if rows else ""
    return newest_media > last_msg


def _needs_own_turn(session_id: int, body) -> bool:
    """Options a steer cannot carry: edit-and-resend, quick, resume, a
    builder, plan/team mode, a new model, or a fresh attachment."""
    if body is None:
        return False
    if (getattr(body, "edit_from_message_id", None) is not None
            or getattr(body, "quick", False)
            or getattr(body, "resume", None) is True
            or getattr(body, "builder", None)
            or getattr(body, "mode", "simple") in ("plan", "team")):
        return True
    return _has_new_attachment(session_id)


def _fold_into_scheduled_agent(session_id: int, text: str, body=None):
    """Steer or stop a scheduled agent that is running in this chat.

    None means the caller starts a normal turn: nothing is running, the
    message is a new question or request, it carries options a steer
    cannot, or the run can no longer take messages (it is ending)."""
    try:
        from aiforge_core.jobs import scheduler as jobs_scheduler
        job_id = jobs_scheduler.running_agent_for_session(session_id)
    except Exception:  # noqa: BLE001
        return None
    if not job_id:
        return None
    from aiforge_core.runtime import chat_interject, chat_store
    from aiforge_core.runtime.run_interrupt import text_cuts_running_work
    if text_cuts_running_work(text):
        chat_store.add_message(session_id, "user", text)
        jobs_scheduler.request_stop(job_id)
        note = "Stopping the scheduled run."
    elif _is_new_request(text) or _needs_own_turn(session_id, body):
        return None
    elif chat_interject.push(session_id, text, require_steerable=True):
        chat_store.add_message(session_id, "user", text)
        note = ("Noted. The scheduled run keeps going and will take this "
                "in. It is not stopped.")
    else:
        # The run is ending and no longer reads messages. Answer it as a
        # normal turn rather than claim the run will take it in.
        return None
    chat_store.add_message(session_id, "assistant", note)

    def _gen():
        yield f"data: {json.dumps({'type': 'message', 'text': note})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return sse_response(_gen(), label=f"chat-sched-{session_id}")
