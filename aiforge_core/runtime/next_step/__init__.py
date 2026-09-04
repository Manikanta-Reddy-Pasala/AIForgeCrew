"""Predicting the next step, and deciding whether to take it.

Three functions and a record. ``predict`` is the only one a chat turn calls; the
other two exist so an outcome can be recorded and shown back to the operator.

Everything fails open. This runs at the end of every turn, and a feature that
improves a good turn must never be able to break one.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from aiforge_core.runtime.next_step import _predict, _repeat, _risk, _store

ACT, OFFER = _risk.ACT, _risk.OFFER


@dataclass(frozen=True)
class Prediction:
    """One predicted next action. ``verdict`` is decided here, never by the model."""
    id: str
    action: str
    tool: str
    args: dict
    confidence: float
    rationale: str
    verdict: str

    def as_event(self) -> dict:
        """The wire shape. ``args`` are deliberately omitted: the UI shows the
        sentence, and the values are the half that may carry a credential."""
        row = asdict(self)
        row.pop("args", None)
        return {"type": "suggestion", **row}


def predict(ctx: dict) -> Prediction | None:
    """The likely next action, or None. Never raises.

    ``ctx`` carries ``message`` (what the user said), ``did`` (what the agent
    did about it), ``repo`` and ``clean_tree``.
    """
    try:
        row = _predict.raw_prediction(ctx or {})
        if row is None:
            return None
        if _predict.is_restatement(row["action"], ctx or {}):
            # A rewording of the request that just finished is not a next step,
            # however sure the model is that it wants it. Dropped BEFORE the
            # confidence floor, because this failure arrives at high confidence
            # by construction: the model is right about what was wanted and
            # wrong only about it still being wanted.
            return None
        if _repeat.suppressed(row["action"], ctx or {}):
            # Already said, or already refused — in this chat or another one.
            return None
        if row["confidence"] < _risk.toolless_floor(row["tool"]):
            # A suggestion that cannot name the tool it would use is advice
            # rather than an action ("consider reviewing the changes"), and it
            # is most of what the feature produced. It is not banned — a strong
            # one is still worth a chip — but it clears a higher bar than a
            # suggestion the system could actually run.
            return None
        if row["confidence"] < _risk.min_confidence():
            # Below the floor nothing is emitted AT ALL, not even an offer: a
            # guess the model itself doubts is noise, and noise teaches the user
            # to ignore the chip that matters.
            return None
        p = Prediction(
            verdict=_risk.verdict(row["tool"], row["args"],
                                  confidence=row["confidence"],
                                  clean_tree=bool((ctx or {}).get("clean_tree"))),
            **{k: row[k] for k in ("id", "action", "tool", "args",
                                   "confidence", "rationale")})
        _store.remember(p, ctx or {})
        return p
    except Exception:  # noqa: BLE001 — see the module docstring
        return None


def outcome(prediction_id: str, accepted: bool, *, edited: str = "") -> None:
    """Record what the user did with a suggestion. Both answers are kept."""
    _store.record_outcome(prediction_id, accepted, edited=edited)


def outcome_row(row: dict, *, accepted: bool) -> None:
    """Record a complete row directly. For replay and for tests."""
    _store.append(row, accepted=accepted)


def history(limit: int = 20) -> list[dict]:
    return _store.history(limit)


__all__ = ["ACT", "OFFER", "Prediction", "predict", "outcome", "outcome_row",
           "history"]
