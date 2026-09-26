"""How long a finished turn waits for its next-step prediction.

The prediction starts beside the answer only on a server with a spare slot
(``_finish._endpoint_one_slot``). The answer and ``done`` are already out when
this waits, so answer latency is unchanged; the wait is short because the
turn keeps its run slot while it lasts.
"""
from __future__ import annotations

import os
import time

_DEFAULT_AFTER_DONE_S = 3.0


def after_done_grace_s() -> float:
    """``AIFORGE_PREDICT_AFTER_DONE_S`` (default 3; 0 = only a prediction
    that has already finished)."""
    try:
        return max(0.0, float(os.environ.get("AIFORGE_PREDICT_AFTER_DONE_S")
                              or _DEFAULT_AFTER_DONE_S))
    except ValueError:
        return _DEFAULT_AFTER_DONE_S


def _stopped(session_id) -> bool:
    if session_id is None:
        return False
    try:
        from aiforge_core.runtime import chat_cancel
        return chat_cancel.is_cancelled(session_id)
    except Exception:  # noqa: BLE001
        return False


def await_ready(ready, session_id=None, grace_s: "float | None" = None) -> bool:
    """Wait up to the grace for ``ready`` (a threading.Event). Stop ends the
    wait. True when it is set."""
    end = time.monotonic() + (after_done_grace_s() if grace_s is None else grace_s)
    while not ready.is_set():
        left = end - time.monotonic()
        if left <= 0 or _stopped(session_id):
            break
        ready.wait(min(0.2, left))
    return ready.is_set()


__all__ = ["after_done_grace_s", "await_ready"]
