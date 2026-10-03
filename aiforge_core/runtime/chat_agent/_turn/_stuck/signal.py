"""What a detector reports: a kind, what repeated, and a short detail."""
from __future__ import annotations

import hashlib
from typing import NamedTuple

#: Every way a run can be seen to go round in circles.
SAME_ACTION = "same_action"          # one call again and again (detail: same | often)
IDENTICAL_RESULT = "identical_result"  # the same call, the same result, in a row
PING_PONG = "ping_pong"              # two calls handing the run back and forth
SAME_OUTPUT = "same_output"          # the same reply, word for word
IDLE_REPLY = "idle_reply"            # replies with no tool (detail: nudge | stop)
MONOLOGUE = "monologue"              # reworded replies with no tool
NARRATION = "narration"              # "I will do X" with no action
SAME_FAILURE = "same_failure"        # the same test failure, fix after fix
NO_PROGRESS = "no_progress"          # steps that change and learn nothing


class Signal(NamedTuple):
    kind: str
    key: str = ""        # what repeated (a call signature, a reply), if one thing
    detail: str = ""     # kind-specific: "same"/"often", "nudge"/"stop", a count


def short_key(sig: str) -> str:
    """A fixed-size key: a signature holds the whole call, file content too."""
    return hashlib.sha1(sig.encode("utf-8", "replace")).hexdigest()  # noqa: S324
