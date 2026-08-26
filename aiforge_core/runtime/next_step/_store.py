"""Predictions and their outcomes, bounded and redacted.

Two rules, and the second matters more:

* **Only ACCEPTED rows become examples.** Dismissals are kept — a feature that
  learns only from its wins drifts, and a dismissal is the clearer signal of the
  two — but they are counters rather than training data.
* **Nothing reaches this file without passing ``memory.sync.redact``.** A row
  the filter refuses is DROPPED, never stored scrubbed. The product has exactly
  one place that judges whether text carries a secret, and this must not become
  a second one with its own opinion.

Argument values are never written at all: the tool NAME is enough to predict
with, and the values are where credentials live.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from aiforge_core.config.paths import config_dir

log = logging.getLogger("aiforge.next_step")

_FILE = "next_step_history.json"

# Enough to show what a user habitually accepts, small enough to stay a glance.
MAX_ROWS = 200


def _path() -> Path:
    d = Path(str(config_dir()))
    d.mkdir(parents=True, exist_ok=True)
    return d / _FILE


def _read() -> list[dict]:
    from aiforge_core.memory.sync import _io

    try:
        rows = _io.read_json(_path()).get("rows") or []
    except Exception:  # noqa: BLE001 — an unreadable store is an empty one
        return []
    return [r for r in rows if isinstance(r, dict)]


def _write(rows: list[dict]) -> None:
    from aiforge_core.memory.sync import _io

    try:
        _io.write_json(_path(), {"rows": rows[-MAX_ROWS:]})
    except Exception as exc:  # noqa: BLE001 — bookkeeping is not the payload
        log.debug("next_step: could not write the history: %s", exc)


def _safe(text: str) -> bool:
    """True when ``text`` may be written. Fails CLOSED.

    ``noise.*`` refusals are ignored on purpose: that stage judges whether a
    note is worth REPLICATING to a fleet, and a prediction trigger is neither a
    note nor replicated — it would trip ``noise.thin`` almost every time.
    ``secrets.*`` and ``private.*`` are what this call is for, and they are
    honoured. The exemption is stated here rather than by loosening the filter,
    which everyone else depends on.
    """
    from aiforge_core.memory.sync import redact

    try:
        v = redact.review({"meta": {"title": ""}, "body": text})
    except Exception:  # noqa: BLE001 — cannot judge it, do not store it
        return False
    return bool(v.send or v.rule.startswith("noise."))


def remember(prediction, ctx: dict) -> None:
    """Record a prediction as pending. Never raises."""
    trigger = str((ctx or {}).get("message") or "")[:300]
    if not _safe(f"{trigger}\n{getattr(prediction, 'action', '')}"):
        log.debug("next_step: prediction not stored — the filter refused it")
        return
    rows = _read()
    rows.append({
        "id": prediction.id, "at": int(time.time()),
        "repo": str((ctx or {}).get("repo") or ""),
        "trigger": trigger, "action": prediction.action,
        "tool": prediction.tool,        # the NAME only; args are never stored
        "verdict": prediction.verdict,
        "confidence": prediction.confidence,
        "accepted": None,               # pending until the user says
        "edited": "",
    })
    _write(rows)


def append(row: dict, *, accepted: bool) -> None:
    """Add a complete row directly. For replay and for tests."""
    rows = _read()
    rows.append({**row, "at": int(time.time()), "accepted": bool(accepted)})
    _write(rows)


def record_outcome(prediction_id: str, accepted: bool, *, edited: str = "") -> None:
    """Mark a prediction accepted or dismissed.

    An unknown id is a no-op: a chip in a browser tab left open across a restart
    is not an error the user can do anything about.
    """
    rows = _read()
    for r in rows:
        if r.get("id") == prediction_id:
            r["accepted"] = bool(accepted)
            r["edited"] = str(edited or "")[:300]
            break
    _write(rows)


def accepted(repo: str, limit: int = 5) -> list[dict]:
    """The most recent accepted predictions for ``repo``, oldest first.

    Per repo on purpose: what a user accepts in one codebase says little about
    another, and mixing them makes every prediction blander.
    """
    rows = [r for r in _read()
            if r.get("accepted") is True and str(r.get("repo") or "") == str(repo)]
    return rows[-limit:] if limit > 0 else []


def history(limit: int = 20) -> list[dict]:
    """Everything recorded, most recent first."""
    return list(reversed(_read()))[:limit]


__all__ = ["MAX_ROWS", "remember", "append", "record_outcome", "accepted",
           "history"]
