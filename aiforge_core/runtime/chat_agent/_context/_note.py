"""The condense note: one message right after the system prompt.

A condense used to fold the breadcrumb, the pinned task and the task board INTO
the system message, and rewrite that message every time. A local model server
caches the prompt by prefix, so changing byte 0 invalidated the whole cache on
every condense. The system message now stays byte-identical for the life of the
run; everything that changes lives in this note (a user-role message, followed
by a one-line assistant acknowledgement when the kept tail opens on a user turn,
so roles keep alternating).

``AIFORGE_STABLE_PREFIX=0`` restores the old layout (everything in convo[0]).
"""
from __future__ import annotations

import os

NOTE_OPEN = "<<AIFORGE_CTX_NOTE>>"
NOTE_CLOSE = "<</AIFORGE_CTX_NOTE>>"
ACK_TEXT = "Understood. Continuing from the note above."
_HEAD = ("[context note — not the user] Earlier turns were condensed. The task, "
         "the task board and what happened are below; continue the task from "
         "here.")


def enabled() -> bool:
    return os.environ.get("AIFORGE_STABLE_PREFIX", "1").strip().lower() \
        not in ("0", "false", "no", "off")


def is_note(m) -> bool:
    return (isinstance(m, dict) and m.get("role") == "user"
            and isinstance(m.get("content"), str)
            and m["content"].startswith(NOTE_OPEN))


def is_ack(m) -> bool:
    return (isinstance(m, dict) and m.get("role") == "assistant"
            and m.get("content") == ACK_TEXT)


def note_index(convo) -> "int | None":
    """1 when ``convo`` has a note right after its system message, else None."""
    if (convo and len(convo) > 1 and isinstance(convo[0], dict)
            and convo[0].get("role") == "system" and is_note(convo[1])):
        return 1
    return None


def prefix_len(convo) -> int:
    """Messages at the front that are the system prompt + the note (+ack)."""
    if note_index(convo) is None:
        return 1
    return 3 if len(convo) > 2 and is_ack(convo[2]) else 2


def text(convo) -> str:
    i = note_index(convo)
    return convo[i]["content"] if i else ""


def build(*blocks: str) -> dict:
    body = "\n\n".join(b for b in blocks if b)
    return {"role": "user",
            "content": f"{NOTE_OPEN}\n{_HEAD}\n\n{body}\n{NOTE_CLOSE}"}


def ack() -> dict:
    return {"role": "assistant", "content": ACK_TEXT}


def with_board(note_text: str, board_block: str, board_re) -> str:
    """``note_text`` with its task-board block replaced by ``board_block``
    (placed before the condense block, or at the end)."""
    from . import _compaction as C
    body = board_re.sub("", note_text)
    at = body.find(C._CONDENSE_OPEN)
    if at < 0:
        at = body.rfind(NOTE_CLOSE)
    if at < 0:
        at = len(body)
    return body[:at].rstrip() + "\n\n" + board_block + "\n\n" + body[at:]


__all__ = ["NOTE_OPEN", "NOTE_CLOSE", "ACK_TEXT", "enabled", "is_note",
           "is_ack", "note_index", "prefix_len", "text", "build", "ack",
           "with_board"]
