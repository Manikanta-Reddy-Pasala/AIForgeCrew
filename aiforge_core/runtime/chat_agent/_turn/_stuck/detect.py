"""Detectors: look at what happened and say whether the run is going round.

Each detector updates its own counters on ``st`` (declared in :mod:`.state`)
and returns a :class:`Signal` or ``None``. They never touch the conversation
and never yield: what to DO about a signal is :mod:`.ladder`'s business.

Today's call order, per phase of a step:

``output``   the model's reply, before it is parsed        -> ``same_output``
``reply``    a reply that ran no tool and did not finish   -> ``monologue``, then ``idle_reply``
``narration`` "I will do X" with no ACTION                 -> ``narration``
``action``   a tool call, before it runs                   -> ``same_action``, then
             ``identical_result``, then ``ping_pong``

(``same_failure`` and ``no_progress`` are judged on a tool's RESULT by
``runtime.same_failure`` / ``runtime.no_progress``; see ``_outcomes`` and
``_idle_steps``.)
"""
from __future__ import annotations

import collections
import hashlib
import json

from aiforge_core.runtime.stuck_policy import Policy

from ..._context import _OUTPUT_REPEAT
from . import signal as K
from .signal import Signal
from .signal import short_key as _short

#: Replies in a row that run no tool before the run counts as going round:
#: above every bounded nudge the final/continue gates can send in a row.
IDLE_REPLIES = 8

_RESULT_KEYS = ("ok", "code", "exit_code", "stdout", "stderr", "error", "content",
                "text", "output")


def _result_hash(result) -> str:
    body = ({k: result.get(k) for k in _RESULT_KEYS if k in result}
            if isinstance(result, dict) else result)
    return hashlib.sha1(json.dumps(body, sort_keys=True, default=str)  # noqa: S324
                        .encode("utf-8", "replace")).hexdigest()


def note_identical(st, sig, result) -> None:
    """Count how many calls IN A ROW were this same call with this same result.
    The workspace fingerprint can keep moving for reasons that have nothing to do
    with the run (a log file, a folder a running service writes to) and so refill
    every other budget; ``sudo rm -f`` of a path that is already gone, run
    forty times, changes nothing and says the same thing each time."""
    key, rh = _short(sig), _result_hash(result)
    recent = getattr(st, "recent_calls", None)
    if recent is None:
        recent = st.recent_calls = collections.deque(maxlen=6)
    recent.append((key, rh))
    prev = getattr(st, "identical_run", None)
    n = prev[2] + 1 if prev and prev[0] == key and prev[1] == rh else 1
    st.identical_run = (key, rh, n)


def identical_repeats(st, sig) -> int:
    """Calls in a row so far that were this call with an unchanged result."""
    prev = getattr(st, "identical_run", None)
    return prev[2] if prev and prev[0] == _short(sig) else 0


def bump_identical(st, sig) -> None:
    """A repeat of an identical-result call was refused (not run): it still
    counts, or a refused call could never reach the change-of-approach step."""
    prev = getattr(st, "identical_run", None)
    if prev and prev[0] == _short(sig):
        st.identical_run = (prev[0], prev[1], prev[2] + 1)


def ping_pong(st, sig=None) -> bool:
    """A, B, A, B, A, B with the same results each time: two calls handing the
    run back and forth. Neither one repeats back to back, so the identical-run
    count never sees it. With ``sig`` (the call about to run) it holds only when
    that call is A or B: a different call breaks the pattern and must run."""
    r = list(getattr(st, "recent_calls", None) or [])
    if len(r) < 6:
        return False
    a, b = r[0], r[1]
    if not (a[0] != b[0] and r[2] == a and r[4] == a and r[3] == b and r[5] == b):
        return False
    return sig is None or _short(sig) in (a[0], b[0])


# ── assistant monologue ────────────────────────────────────────────────────
# OpenHands' third stuck pattern: the model answers three times in a row, runs
# no tool, and says essentially the same thing each time. Word-for-word
# repeats are the stuck-output guard's; this catches the reworded ones.

def _normalised(text: str) -> str:
    """Case, punctuation, digits and spacing removed: two replies that differ
    only in those are the same thing said again."""
    import re
    words = re.sub(r"[^a-z\s]+", " ", str(text or "").lower()).split()
    return " ".join(words)[:800]


def similar_text(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    import difflib
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    floor = Policy.load().monologue_similarity
    return sm.real_quick_ratio() >= floor and sm.quick_ratio() >= floor \
        and sm.ratio() >= floor


def note_monologue(st, text: str) -> bool:
    """Record one reply that ran no tool. True when the last N such replies
    (N = ``Policy.monologue_repeats``) all say essentially the same thing."""
    n = Policy.load().monologue_repeats
    if n <= 0:
        return False
    recent = getattr(st, "monologue", None)
    if recent is None:
        recent = st.monologue = collections.deque(maxlen=n)
    elif recent.maxlen != n:
        recent = st.monologue = collections.deque(recent, maxlen=n)
    norm = _normalised(text)
    if not norm:
        return False
    recent.append(norm)
    if len(recent) < n:
        return False
    items = list(recent)
    return all(similar_text(items[i], items[i + 1]) for i in range(n - 1))


def reset_monologue(st) -> None:
    recent = getattr(st, "monologue", None)
    if recent is not None:
        recent.clear()


# ── the detectors a guard calls ────────────────────────────────────────────

def same_output(st, out: str) -> Signal | None:
    """The model sent the same reply (word for word) ``_OUTPUT_REPEAT`` times
    running."""
    st.recent_outputs.append(out.strip())
    if (len(st.recent_outputs) == _OUTPUT_REPEAT
            and len(set(st.recent_outputs)) == 1):
        return Signal(K.SAME_OUTPUT, out.strip())
    return None


def idle_reply(st, text: str) -> Signal | None:
    """A reply that ran no tool and did not end the turn. A monologue (reworded
    repeats) trips at once; otherwise every ``IDLE_REPLIES`` such replies trip
    once: first ``nudge``, then ``stop``. Any tool call resets the counts."""
    st.idle_replies = getattr(st, "idle_replies", 0) + 1
    if not Policy.load().pause_on_stuck and note_monologue(st, text):
        st.idle_replies = 0
        reset_monologue(st)
        return Signal(K.MONOLOGUE, text)
    if st.idle_replies < IDLE_REPLIES:
        return None
    st.idle_replies = 0
    st.idle_trips = getattr(st, "idle_trips", 0) + 1
    return Signal(K.IDLE_REPLY, "", "nudge" if st.idle_trips == 1 else "stop")


def narration(st) -> Signal | None:
    """The model described a next step and ran nothing: the third time in a
    row trips."""
    st.continue_nudges += 1
    return Signal(K.NARRATION, "", str(st.continue_nudges)) \
        if st.continue_nudges > 2 else None


def action(st, sig: str, looping: str, duplicate: bool) -> Signal | None:
    """A tool call about to run. ``looping`` is the workspace-aware repeat
    count's verdict (``_progress.strike``: "", "same" or "often");
    identical results in a row and a ping-pong catch what it cannot."""
    if looping:
        return Signal(K.SAME_ACTION, sig, looping)
    if duplicate:
        return None
    if identical_repeats(st, sig) >= Policy.load().identical_repeats:
        return Signal(K.IDENTICAL_RESULT, sig, "same")
    if ping_pong(st, sig):
        return Signal(K.PING_PONG, sig, "same")
    return None


#: phase -> detector. A phase's detector is called with the phase's event.
DETECTORS = {"output": same_output, "reply": idle_reply,
             "narration": narration, "action": action}


def detect(phase: str, st, *event) -> Signal | None:
    return DETECTORS[phase](st, *event)
