"""Every text/event-stream response must defeat proxy buffering.

Without ``X-Accel-Buffering: no`` nginx buffers the stream (proxy_buffering is
on by default), holding the per-step events and the 10s keepalive ping, so the
browser / Cloudflare sees a silent connection and drops it at the proxy read
timeout — the "Agent error: network error" the UI showed on any turn longer
than that timeout. This asserts the header at the response layer so a future
SSE route that forgets it is caught here, not in production.
"""
from __future__ import annotations

from aiforge_core.api.routes._sse import SSE_HEADERS, sse_response


def _gen():
    yield "data: {}\n\n"


def test_sse_response_sets_the_anti_buffering_headers():
    r = sse_response(_gen())
    assert r.media_type == "text/event-stream"
    # The load-bearing one: nginx streams instead of buffering.
    assert r.headers["x-accel-buffering"] == "no"
    assert r.headers["cache-control"] == "no-cache"


def test_sse_headers_constant_is_the_contract():
    assert SSE_HEADERS["X-Accel-Buffering"] == "no"
    assert SSE_HEADERS["Cache-Control"] == "no-cache"
    # Hop-by-hop headers are the ASGI server's job — must not be set here.
    assert "Connection" not in SSE_HEADERS


def test_every_sse_route_goes_through_the_helper():
    """A guard against a new StreamingResponse(text/event-stream) that bypasses
    sse_response and silently reintroduces the buffering bug."""
    import pathlib
    import re

    routes = pathlib.Path("aiforge_core/api/routes")
    offenders = []
    for f in routes.glob("*.py"):
        if f.name == "_sse.py":
            continue
        text = f.read_text(encoding="utf-8")
        # A StreamingResponse constructed with the SSE media type directly is
        # the bug; the helper is the only sanctioned construction.
        for m in re.finditer(r"StreamingResponse\([^)]*text/event-stream",
                             text, re.S):
            offenders.append(f"{f.name}: {m.group(0)[:60]}")
    assert not offenders, (
        "SSE responses must use sse_response() (sets X-Accel-Buffering): "
        + "; ".join(offenders))
