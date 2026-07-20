"""Shared construction for Server-Sent-Events responses.

``StreamingResponse(..., media_type="text/event-stream")`` is not enough to
actually STREAM through a proxy. nginx buffers proxied responses by default
(``proxy_buffering on``), so it holds every per-step event AND the 10s keepalive
ping (see ``chat_pipeline`` / ``chat_runs``) instead of forwarding them. The
browser — or Cloudflare in the ``CF → nginx → host`` deploy — then sees a
silent connection and drops it at the proxy's read timeout, which the UI
surfaces as "Agent error: network error" on any turn longer than that timeout.
This is why the app-level heartbeat was necessary but NOT sufficient: the ping
has to reach the wire, and ``X-Accel-Buffering: no`` is what tells nginx to
flush this response instead of buffering it. ``Cache-Control: no-cache`` is the
standard SSE hint so nothing along the path caches the stream.

Hop-by-hop headers (``Connection``) are deliberately omitted — the ASGI server
owns those, and setting them here draws h11 warnings for no benefit.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Iterator

from fastapi.responses import StreamingResponse

_log = logging.getLogger("aiforge.sse")

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",     # nginx: stream this response, do not buffer
}


def _instrumented(generator: Iterator, label: str) -> Iterator:
    """Wrap an SSE generator so how it ENDS is visible in the server log.

    A "network error" in the browser is silent on the server unless we record
    it: when the client (or a proxy) drops the connection mid-stream, Starlette
    calls ``.close()`` on this generator, raising ``GeneratorExit`` at the yield
    — so a disconnect after N events at T seconds is exactly what a mid-stream
    drop looks like from here. Correlating that line's timestamp with the user's
    error pins the mechanism: a ``client disconnected`` line means the
    connection died (network/proxy); a ``failed`` line means the app raised; and
    a ``completed`` line while the browser still errored means the drop is purely
    downstream (proxy buffering/timeout), not the app.
    """
    start = time.monotonic()
    n = 0
    try:
        for item in generator:
            n += 1
            yield item
    except GeneratorExit:
        _log.info("sse %s: client disconnected after %.1fs, %d events",
                  label, time.monotonic() - start, n)
        raise
    except Exception as exc:  # noqa: BLE001 — re-raised; we only annotate it
        _log.warning("sse %s: stream failed after %.1fs, %d events: %s",
                     label, time.monotonic() - start, n, exc)
        raise
    else:
        _log.info("sse %s: completed after %.1fs, %d events",
                  label, time.monotonic() - start, n)


def sse_response(generator: Iterator, *, label: str = "sse") -> StreamingResponse:
    """A ``text/event-stream`` response that survives a buffering proxy and logs
    how it ended (see :func:`_instrumented`)."""
    return StreamingResponse(_instrumented(generator, label),
                             media_type="text/event-stream",
                             headers=dict(SSE_HEADERS))
