"""Every counter the stuck / loop guards keep, in ONE place, and every reset.

The fields used to be created lazily with ``getattr``/``setattr`` all over the
guards, so each reset path had to remember them all. Now a field is declared
here once, with its default, and a reset names a *scope*:

* ``action``     a tool ran: the narration, idle-reply and monologue counts
                 start over;
* ``recovery``   after a recap nudge: forget the recent-calls window (the
                 identical-run COUNT is kept: a refused repeat keeps raising it
                 until the change of approach);
* ``escalation`` a change of approach was sent: same as ``recovery``;
* ``restart``    the context was replaced by a handoff: also forget the
                 identical-run count and the recent-outputs window.

A finished task-board item clears none of this (a recovery budget refills in
``may_recover`` from real progress), so there is no ``item`` scope.

The field names are the old ``st.<field>`` names: the loop state exposes them
as aliases of ``st.stuck.<field>`` and the guards still read them off ``st``.
"""
from __future__ import annotations

import collections
import types
from dataclasses import dataclass, field, fields

from ..._context import _OUTPUT_REPEAT


def _od():
    return collections.OrderedDict()


@dataclass
class StuckState:
    # same action: per-state strikes, since-last-new-state, whole-run counts
    strikes: collections.OrderedDict = field(default_factory=_od)
    backstop: collections.OrderedDict = field(default_factory=_od)
    lifetime: collections.OrderedDict = field(default_factory=_od)
    #: actions whose lifetime count a recovery already reset once
    lifetime_forgiven: collections.OrderedDict = field(default_factory=_od)
    action_counts: collections.OrderedDict = field(default_factory=_od)
    # identical result in a row / ping-pong
    recent_calls: collections.deque = field(
        default_factory=lambda: collections.deque(maxlen=6))
    identical_run: tuple | None = None
    # the same reply word for word
    recent_outputs: collections.deque = field(
        default_factory=lambda: collections.deque(maxlen=_OUTPUT_REPEAT))
    # replies that run no tool
    monologue: collections.deque | None = None
    idle_replies: int = 0
    idle_trips: int = 0
    continue_nudges: int = 0
    # recovery budgets (recap nudges)
    stuck_recoveries: int = 0
    recoveries_total: int = 0
    recovery_mark: tuple | None = None
    recoveries_closed_mark: int | None = None
    # change of approach
    stuck_escalations: int = 0
    restarts: int = 0
    reason_boost: int = 0
    failed_approaches: list = field(default_factory=list)
    # the same-failure and no-progress rules: lives here, not in the
    # conversation, so a condense cannot reset it
    same_fail: dict = field(default_factory=dict)
    np_mark: tuple = (0, 0, 0, 0, 0)
    np_seen: collections.OrderedDict = field(default_factory=_od)
    np_fails: int | None = None

    def reset(self, scope: str) -> None:
        reset(self, scope)


FIELD_NAMES = frozenset(f.name for f in fields(StuckState))

_CLEAR = object()           # call .clear() on the field when it has one

_SCOPES = {
    "action": {"continue_nudges": 0, "idle_replies": 0, "idle_trips": 0,
               "monologue": _CLEAR},
    "recovery": {"recent_calls": _CLEAR},
    "escalation": {"recent_calls": _CLEAR},
    "restart": {"recent_calls": _CLEAR, "recent_outputs": _CLEAR,
                "identical_run": None},
}


def reset(target, scope: str) -> None:
    """Reset ``scope`` on ``target``: a :class:`StuckState`, or any object that
    carries the same field names (a loop state, a test's namespace)."""
    for name, to in _SCOPES[scope].items():
        if to is _CLEAR:
            held = getattr(target, name, None)
            if hasattr(held, "clear"):
                held.clear()
        else:
            setattr(target, name, to)


def _alias(name: str) -> property:
    return property(lambda self: getattr(self.stuck, name),
                    lambda self, value: setattr(self.stuck, name, value))


class StuckAliases:
    """Mixin: ``st.<field>`` reads and writes ``st.stuck.<field>``."""


for _name in FIELD_NAMES:
    setattr(StuckAliases, _name, _alias(_name))


class LoopState(StuckAliases, types.SimpleNamespace):
    """The chat loop's state namespace; its stuck fields live in ``stuck``."""

    def __init__(self, **kw):
        own = {k: kw.pop(k) for k in list(kw) if k in FIELD_NAMES}
        super().__init__(stuck=StuckState(**own), **kw)
