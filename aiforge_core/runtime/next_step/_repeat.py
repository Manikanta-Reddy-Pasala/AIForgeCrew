"""Do not say the same thing twice — in this chat or the next one.

The complaint this exists for: the same two or three suggestions arriving in
chat after chat. The cause was not the model being repetitive. It was that
every prediction started from an empty room — the history file recorded what
had been offered and what the user had said to it, and the prediction path
consulted exactly one slice of that (accepted rows, as examples). A dismissal
changed nothing, and an offer nobody had answered yet changed nothing, so the
next turn in the next session proposed it again with full confidence.

Two windows, because the two cases mean different things:

* **Offered recently** (default 24h) — the user has seen this sentence and has
  not asked for it. Repeating it is noise, whatever they eventually click.
* **Dismissed** (default 14 days) — the user said no. That is a stronger signal
  than a shrug and deserves a longer silence, but not a permanent one: a
  suggestion that was wrong in March can be right in June, and a store that
  never forgets becomes a list of things the product may never say again.

Matching is by CONTENT WORDS, not string equality. The model rewords itself
every turn, and "run the tests" / "run the test suite" / "run tests for the
chat module" are one suggestion wearing three sentences — the rewording is
precisely how the repeat got past a naive check.

``AIFORGE_PREDICT_REPEAT_H=0`` / ``AIFORGE_PREDICT_DISMISS_DAYS=0`` turn each
window off.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("aiforge.next_step")

_DEFAULT_REPEAT_H = 24.0
_DEFAULT_DISMISS_DAYS = 14.0


def _hours(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name) or default))
    except ValueError:
        return default


def repeat_window_s() -> float:
    return _hours("AIFORGE_PREDICT_REPEAT_H", _DEFAULT_REPEAT_H) * 3600.0


def dismiss_window_s() -> float:
    return _hours("AIFORGE_PREDICT_DISMISS_DAYS", _DEFAULT_DISMISS_DAYS) * 86400.0


def suppressed(action: str, ctx: dict) -> bool:
    """True when this suggestion has already been made, or already refused.

    Fails OPEN: an unreadable history means the feature behaves as it did
    before this file existed, which is worse than silence but not broken.
    """
    from aiforge_core.runtime.next_step import _store

    repo = str((ctx or {}).get("repo") or "")
    try:
        dismiss_s = dismiss_window_s()
        if dismiss_s > 0:
            row = _store.recent_for(repo, action, within_s=dismiss_s)
            if row is not None and row.get("accepted") is False:
                log.debug("next_step: suppressed — dismissed before: %r", action)
                return True
        repeat_s = repeat_window_s()
        if repeat_s > 0:
            row = _store.recent_for(repo, action, within_s=repeat_s)
            if row is not None:
                # Includes rows the user ACCEPTED: they asked for it once and it
                # ran, so proposing it again an hour later is the same noise
                # from the other direction.
                log.debug("next_step: suppressed — offered %s ago: %r",
                          repeat_s, action)
                return True
    except Exception:  # noqa: BLE001 — see the docstring
        return False
    return False


__all__ = ["dismiss_window_s", "repeat_window_s", "suppressed"]
