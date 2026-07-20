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

from collections.abc import Iterator

from fastapi.responses import StreamingResponse

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",     # nginx: stream this response, do not buffer
}


def sse_response(generator: Iterator) -> StreamingResponse:
    """A ``text/event-stream`` response that survives a buffering proxy."""
    return StreamingResponse(generator, media_type="text/event-stream",
                             headers=dict(SSE_HEADERS))
