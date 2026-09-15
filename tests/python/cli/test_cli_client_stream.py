"""The client against a stub API — framing, 409 and a dropped stream."""

from __future__ import annotations

import httpx
import pytest
from aiforge_cli import client as api


def _sse(events: list[str]) -> bytes:
    return "".join(f"data: {e}\n\n" for e in events).encode()


def _client(handler) -> api.Client:
    c = api.Client("http://127.0.0.1:8799")
    c._http = httpx.Client(base_url="http://127.0.0.1:8799",
                           transport=httpx.MockTransport(handler))
    return c


def test_events_arrive_as_dicts_in_order():
    body = _sse(['{"type": "thought", "text": "hi"}', '{"type": "done"}'])

    def handler(request):
        assert request.url.path == "/api/chat/sessions/3/message"
        return httpx.Response(200, content=body,
                              headers={"content-type": "text/event-stream"})

    got = list(_client(handler).send(3, "go"))
    assert [e["type"] for e in got] == ["thought", "done"]


def test_framing_and_junk_lines_are_skipped():
    body = (b": keepalive comment\n\n"
            b"data: \n\n"
            b"data: not json\n\n"
            b'data: {"type": "done"}\n\n')

    def handler(_request):
        return httpx.Response(200, content=body,
                              headers={"content-type": "text/event-stream"})

    assert [e["type"] for e in _client(handler).send(1, "x")] == ["done"]


def test_a_second_producer_is_told_the_session_is_busy():
    def handler(_request):
        return httpx.Response(409, json={"detail": "a run is already in progress"})

    with pytest.raises(api.Busy):
        list(_client(handler).send(1, "x"))


def test_a_dead_api_raises_apidown_not_a_transport_error():
    def handler(_request):
        raise httpx.ConnectError("connection refused")

    with pytest.raises(api.ApiDown):
        _client(handler).sessions()


def test_healthy_is_false_rather_than_raising():
    def handler(_request):
        raise httpx.ConnectError("connection refused")

    assert _client(handler).healthy() is False


def test_a_stalled_stream_is_a_stalled_error():
    def handler(_request):
        raise httpx.ReadTimeout("timed out")

    with pytest.raises(api.Stalled):
        list(_client(handler).attach(4))


def test_parse_sse_line_only_accepts_data_objects():
    assert api.parse_sse_line('data: {"type": "ping"}') == {"type": "ping"}
    assert api.parse_sse_line("event: ping") is None
    assert api.parse_sse_line("data: [1, 2]") is None
    assert api.parse_sse_line("") is None


def test_a_slow_health_reply_does_not_mean_the_box_is_missing():
    # /api/health answers in 0.1s idle and 3s under load on a real box. Timing
    # that out had the CLI decide nothing was there and start a second sandbox.
    def handler(_request):
        raise httpx.ReadTimeout("slow")

    assert _client(handler).healthy() is True


def test_a_refused_connection_does_mean_the_box_is_missing():
    def handler(_request):
        raise httpx.ConnectError("connection refused")

    assert _client(handler).healthy() is False


def test_the_client_ignores_a_proxy_from_the_environment(monkeypatch):
    # It only ever talks to 127.0.0.1; an inherited proxy would route loopback
    # through it and fail.
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.internal:3128")
    c = api.Client("http://127.0.0.1:8799")
    try:
        assert c._http.trust_env is False
    finally:
        c.close()
