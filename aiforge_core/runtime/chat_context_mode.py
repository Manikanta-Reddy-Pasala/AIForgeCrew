"""How a chat holds its context: one rolling context, or split into subtasks.

* ``one`` (the default): one agent does the whole request in one context. It
  keeps what it read and ran across the chat's messages
  (``runtime/chat_transcript``), and the history shrinks only at the condense
  point. A build-shaped request is NOT split into subtasks.
* ``split``: a multi-file build request is broken into subtasks, each run in a
  fresh context of its own (``runtime/parallel_subtasks``) — the old default.
  Any other turn still runs as one agent and keeps its context.

Clients that send no ``context`` (VS Code, the CLI, side tasks) get the
server default.

The user picks it per chat (the message's ``context`` field); a message that
says nothing gets ``AIFORGE_CHAT_CONTEXT`` (``one`` when unset).
"""
from __future__ import annotations

import os

ONE = "one"
SPLIT = "split"


def resolve(value=None) -> str:
    v = str(value or os.environ.get("AIFORGE_CHAT_CONTEXT", ONE)).strip().lower()
    return SPLIT if v in ("split", "subtasks", "multi", "multiple") else ONE


def is_split(value=None) -> bool:
    return resolve(value) == SPLIT
