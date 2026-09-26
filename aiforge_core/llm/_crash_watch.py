"""Does THIS request crash the model server? (llm/request_health module doc)

Split from :mod:`aiforge_core.llm.model_wait`. The evidence must be clear —
anything ambiguous is an outage, and an outage is waited for forever.
"""
from __future__ import annotations

import logging
import time

from . import model_outage, request_health

log = logging.getLogger("aiforge.llm.model_wait")


def judge(waiter, exc: BaseException, sent: "float | None",
          failed_at: float) -> None:
    """The model server's own host refuses connections right after this
    request: count a crash cycle when the failure itself was the server
    dropping this request within the crash window of its send; otherwise the
    count starts again. Raises LLMRequestFailing after enough cycles in a
    row."""
    from ._wait_scope import LLMRequestFailing
    h = waiter.health
    if sent is None or failed_at - sent > request_health.crash_window_s() \
            or not model_outage.crash_evidence(exc):
        h.crashes = 0
        return
    answered = request_health.last_answer(waiter.url)
    if answered is not None and h.crash_at is not None \
            and answered > h.crash_at:
        h.crashes = 0                 # the endpoint answered in between
    h.crashes += 1
    h.crash_at = time.monotonic()
    request_health.track_crashes(waiter.url, True)
    if h.crashes < request_health.crash_resends():
        return
    log.warning("llm.request_crashes url=%s n=%d err=%.200s", waiter.url,
                h.crashes, exc)
    err = LLMRequestFailing(
        waiter.url, h.crashes, exc,
        f"LLM issue: the model server at {waiter.url or '?'} went down "
        f"within seconds of this request, {h.crashes} times in a row — "
        "the request crashes the model server")
    err.cause = "request crashes the model server"
    raise err from exc


def note_answer(waiter, url: str, endpoint) -> None:
    """A real answer on an endpoint resets every crash count on it."""
    try:
        if not request_health.crashes_tracked():
            return
        if waiter is not None:
            url = waiter.url
        elif not url and endpoint is not None:
            url = endpoint()[0]
        request_health.note_answer(url)
    except Exception:  # noqa: BLE001
        pass


__all__ = ["judge", "note_answer"]
