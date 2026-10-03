"""The ways a chat history gets smaller, behind one interface.

Two things shrink the prompt in place, each with its own trigger and knobs:

* :class:`AgeOld`   - old read/command output becomes a head, a tail and an id
  (``_aging``; cheap, runs every step, never drops a message).
* :class:`Condense` - the middle of the history becomes one note (``_compaction``;
  runs when the history is over the budget).

(The per-item reset in ``_turn/_items`` and the stuck-run restart in
``_turn/_escalate`` replace the whole history, so they stay with the loop code
that knows when they are due.)

They all take a conversation and return a conversation, so a caller that
wants "make it fit" runs them cheapest first and stops when it does
(:func:`reduce_to_budget`). The budget is a number of characters of history
(0 = no limit), the same unit ``_window._ctx_budget_chars`` returns.

This module only adapts: the behaviour (what is kept, the text of a note)
stays in the modules above.
"""
from __future__ import annotations

from typing import Protocol


class Reducer(Protocol):
    name: str

    def applies(self, convo: list, budget: int) -> bool:
        """True when running this reducer on ``convo`` could help."""

    def reduce(self, convo: list, budget: int) -> list:
        """``convo`` made smaller (the same list when nothing was done)."""


def history_chars(convo: list) -> int:
    """Characters of history: everything after the system message."""
    from ._compaction import _hist_chars
    return _hist_chars(convo[1:])


def fits(convo: list, budget: int) -> bool:
    return budget <= 0 or history_chars(convo) <= budget


class AgeOld:
    """Shrink old, large read/search observations (in place)."""

    name = "age"

    def __init__(self, protect_from: "int | None" = None,
                 forget: "set | None" = None) -> None:
        self.protect_from = protect_from
        self.forget = forget

    def applies(self, convo: list, budget: int) -> bool:
        return bool(convo)

    def reduce(self, convo: list, budget: int = 0) -> list:
        from ._aging import age_observations
        age_observations(convo, protect_from=self.protect_from,
                         forget=self.forget)
        return convo


class Condense:
    """Fold the middle of the history into one note when it is over budget.

    ``compact`` holds the keyword arguments of ``_compact_convo`` (role, tail
    size, pin, handoff state, ...). That function reads the budget itself from
    ``role``; ``budget`` here only decides whether the reducer applies.
    """

    name = "condense"

    def __init__(self, **compact) -> None:
        self.compact = compact

    def applies(self, convo: list, budget: int) -> bool:
        return self.compact.get("force", False) or not fits(convo, budget)

    def reduce(self, convo: list, budget: int = 0) -> list:
        from ._compaction import _compact_convo
        return _compact_convo(convo, **self.compact)


def reduce_to_budget(convo: list, budget: int, reducers) -> list:
    """Run ``reducers`` in order (cheapest first), stopping once ``convo`` fits
    ``budget``. A reducer that does not apply is skipped."""
    for r in reducers:
        if fits(convo, budget):
            break
        if r.applies(convo, budget):
            convo = r.reduce(convo, budget)
    return convo


__all__ = ["Reducer", "AgeOld", "Condense", "history_chars", "fits",
           "reduce_to_budget"]
