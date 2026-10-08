"""The working transcript of a chat turn, carried to the next message.

Between messages a chat kept only what was SAID: the user's text and the
assistant's final answers. Every file the model read and every command output
was gone at the next message, so a follow-up about the same code read it all
again.

The conversation the loop worked in
(its tool calls and their results, everything after the system prompt) is
saved when a turn ends, and the next message of the same chat starts from it
instead of from the bare answers. The system prompt is rebuilt as before. The
usual condense keeps it inside the window. Only a chat in one-context mode
(``runtime/chat_context_mode``) carries it; ``AIFORGE_CHAT_CARRY_TRANSCRIPT=0``
turns it off.

The saved transcript is used only when it is the chat's latest history: it
records how many user messages the turn had and the user's own words of the
turn, and the next turn must have exactly one more message whose previous one
says the same (the harness's additions to a message — the enhancer's
restatement, a resume brief, a draft-only note — are not compared). An edit-and-resend, a turn that never
saved, or anything else that changed the history falls back to the old way.

What is kept is text: an image part is dropped (the next turn may be on a
text-only model). The condense note stays — it holds what earlier condenses
folded away (the summary, offload ids, files touched) — without the old turn's
task board. The message's own prompt-block note and the action log go: the
next message gets fresh ones.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger("aiforge.chat_transcript")

#: Where the enhancer's restatement starts in a user message.
_ENHANCED = "\n\n---\n[Interpreted request"

#: A transcript bigger than this is not kept (bytes of JSON).
MAX_BYTES = 6_000_000

_lock = threading.Lock()


def enabled() -> bool:
    return os.environ.get("AIFORGE_CHAT_CARRY_TRANSCRIPT", "1").strip().lower() \
        not in ("0", "false", "no", "off")


def _dir() -> Path:
    root = Path(os.environ.get("AIFORGE_CONFIG_DIR",
                               os.path.expanduser("~/.aiforge")))
    return root / "chat_transcripts"


def _path(session_id) -> Path:
    return _dir() / f"session_{int(session_id)}.json"


def _user_count(messages) -> int:
    return sum(1 for m in messages or () if isinstance(m, dict) and m.get("role") == "user")


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # A prompt-block note added as its own part is the harness's, not text.
        return "\n".join(p["text"] for p in content
                         if isinstance(p, dict) and isinstance(p.get("text"), str)
                         and not p["text"].startswith("<<AIFORGE_TURN_CONTEXT>>"))
    return ""


def _users(messages) -> list:
    return [m for m in messages or () if isinstance(m, dict) and m.get("role") == "user"]


#: How much of the user's words is kept to recognise the turn again.
_MARK_CHARS = 2000


def _mark(message) -> str:
    """The user's own words of a message: what comes before the first block
    the harness adds (they start with a ``---`` line)."""
    words = _text((message or {}).get("content")).split("\n\n---\n")[0]
    return " ".join(words.split())[:_MARK_CHARS]


def _same_words(saved: str, now: str) -> bool:
    """One may carry a note the other does not (added at the end)."""
    if not saved or not now:
        return saved == now
    return saved == now or saved.startswith(now) or now.startswith(saved)


def _is_turn_note(m) -> bool:
    try:
        from aiforge_core.runtime.chat_agent._turn._convo import is_turn_note
        return is_turn_note(m)
    except Exception:  # noqa: BLE001
        return False


_CARRIED_HEAD = ("[context note — not the user] Earlier messages of this chat "
                 "were condensed; what they did is summarised below. The task "
                 "is the newest message.")


def _condense_note(m) -> "str | None":
    """What the condense note keeps of the earlier work — the summary, offload
    ids, files touched — without what belonged to that turn alone: its task
    board, its pinned goal, its "continue the task" heading and the prompt
    blocks re-pinned after it. None when ``m`` is not that note."""
    import re
    try:
        from aiforge_core.runtime.chat_agent._context import _note
        from aiforge_core.runtime.chat_agent._context._compaction import _GOAL_RE, _board_re
        if not _note.is_note(m):
            return None
        text = m["content"]
        end = text.find(_note.NOTE_CLOSE)
        if end >= 0:
            text = text[:end + len(_note.NOTE_CLOSE)]
        text = _board_re().sub("", _GOAL_RE.sub("", text))
        text = text.replace(_note._HEAD, _CARRIED_HEAD)
        return re.sub(r"\n{3,}", "\n\n", text)
    except Exception:  # noqa: BLE001
        return None


def _is_action_log_note(m) -> bool:
    try:
        from aiforge_core.runtime import action_log
        return action_log.is_note(m)
    except Exception:  # noqa: BLE001
        return False


def _working_part(convo) -> list:
    """``convo`` as text, without its system prompt, the action-log notes
    (the next turn inserts a fresh one) and the condense note."""
    acks = set()
    try:
        from aiforge_core.runtime.action_log import ACK_TEXT
        acks.add(ACK_TEXT)
    except Exception:  # noqa: BLE001
        pass
    try:
        from aiforge_core.runtime.chat_agent._context._note import ACK_TEXT as NOTE_ACK
        from aiforge_core.runtime.chat_agent._turn._convo import TURN_NOTE_ACK
        acks.update({NOTE_ACK, TURN_NOTE_ACK})
    except Exception:  # noqa: BLE001
        pass
    out: list = []
    skip_ack = False
    for m in convo[1:] if convo and convo[0].get("role") == "system" else convo or ():
        if not isinstance(m, dict):
            continue
        kept_note = _condense_note(m)
        if kept_note is not None:
            out.append({"role": "user", "content": kept_note})
            continue
        if _is_action_log_note(m) or _is_turn_note(m):
            skip_ack = True
            continue
        if skip_ack and m.get("role") == "assistant" and m.get("content") in acks:
            skip_ack = False
            continue
        skip_ack = False
        text = _text(m.get("content"))
        if m.get("role") == "user":
            # The enhancer's restatement and a prompt-block note added at the
            # end served that message; the user's words stay.
            for cut in (_ENHANCED, "<<AIFORGE_TURN_CONTEXT>>"):
                if cut in text:
                    text = text.split(cut)[0].rstrip()
        out.append({"role": m.get("role"), "content": text})
    # The conversation opens on the user's side (a condense tail can start on
    # an answer), and turns alternate: a dropped note must not leave two of a
    # kind in a row.
    if out and out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": "(the earlier part of this chat continues below)"})
    merged: list = []
    for m in out:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1]["content"] += "\n\n" + m["content"]
        else:
            merged.append(m)
    return merged


def save(session_id, messages, convo, answer: "str | None" = None,
         role: str = "doer") -> bool:
    """Keep ``convo`` as the transcript of the turn that started from
    ``messages``; ``answer`` is the turn's final text, added when the loop
    did not leave it in ``convo``. True when written. A turn that ended
    without an answer (stopped, failed, waiting on the user) drops the old
    transcript instead, so the next message falls back to the bare history."""
    if not enabled() or session_id is None or not convo:
        return False
    work = _working_part(convo)
    if work and work[-1].get("role") != "assistant" and answer:
        work.append({"role": "assistant", "content": answer})
    if not answer or not work or work[-1].get("role") != "assistant":
        drop(session_id)
        return False
    try:
        try:
            from aiforge_core.llm import ctx_ratio
            cpt = ctx_ratio.chars_per_token(role)
        except Exception:  # noqa: BLE001
            cpt = None
        body = json.dumps({"users": _user_count(messages), "cpt": cpt,
                           "mark": _mark(_users(messages)[-1]) if _users(messages) else "",
                           "messages": work})
    except (TypeError, ValueError):
        return False
    if len(body) > MAX_BYTES:
        return False
    try:
        with _lock:
            d = _dir()
            d.mkdir(parents=True, exist_ok=True)
            tmp = _path(session_id).with_suffix(".tmp")
            tmp.write_text(body, encoding="utf-8")
            os.replace(tmp, _path(session_id))
        return True
    except OSError as exc:
        log.debug("transcript not saved: %s", exc)
        return False


def saved_ratio(session_id) -> "float | None":
    """The characters-per-token ratio measured when the transcript was saved."""
    try:
        data = json.loads(_path(session_id).read_text(encoding="utf-8"))
        return float(data.get("cpt")) if data.get("cpt") else None
    except (OSError, ValueError, TypeError):
        return None


def carried(session_id, messages) -> "list | None":
    """``messages`` with the earlier turns replaced by the saved transcript,
    or ``None`` when there is none that fits."""
    if not enabled() or session_id is None or not messages:
        return None
    last = messages[-1]
    if not isinstance(last, dict) or last.get("role") != "user":
        return None
    try:
        data = json.loads(_path(session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    work = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(work, list) or not work:
        return None
    users = _users(messages)
    if len(users) != int(data.get("users") or 0) + 1 or len(users) < 2:
        return None              # the history changed since it was saved
    if not _same_words(str(data.get("mark") or ""), _mark(users[-2])):
        return None
    # What was posted to the chat after the turn (a background command that
    # finished, a hook note) sits in the history after the answer — merged
    # into it — and not in the saved transcript: the agent must see it.
    prev = messages[-2] if len(messages) >= 2 else None
    if (isinstance(prev, dict) and prev.get("role") == "assistant"
            and work[-1].get("role") == "assistant"):
        said = _text(prev.get("content")).strip()
        answer = str(work[-1].get("content") or "").strip()
        extra = ""
        if answer and said.startswith(answer):
            extra = said[len(answer):]
        elif answer and answer in said:
            extra = said.split(answer, 1)[1]
        if extra.strip():
            # Only what came after the answer is added; the answer stays as it was.
            work = [*work[:-1], {"role": "assistant",
                                 "content": answer + "\n\n" + extra.strip()}]
    return [*work, last]


def drop(session_id) -> None:
    try:
        _path(session_id).unlink()
    except (OSError, TypeError, ValueError):
        pass
