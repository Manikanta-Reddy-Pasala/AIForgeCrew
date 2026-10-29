"""A call that goes silent is never silent for the user, and never endless.

The failure seen on a WSL / Docker install: a long prefill sends no byte, the
network drops the idle connection, every stall bound fires in silence and each
re-send waits twice as long (3, 6, 12, 24 minutes) — "No output for 41m". A
proxy that sends only keep-alive comments for an upstream that is gone held
the call forever. Real local sockets, as in test_llm_stream_health."""
import socket

import pytest

from aiforge_core.llm import _wait_scope, request_health
from aiforge_core.llm.client import _http, _http_retry
from aiforge_core.llm.client._stream_health import LLMStreamStalled
from aiforge_core.llm.model_outage import LLMRequestFailing
from tests.python.llm.test_llm_stream_health import _call, _serve


def test_keep_alive_comments_alone_do_not_keep_a_dead_call_alive(monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_FIRST_TOKEN_S", "1")
    monkeypatch.setenv("AIFORGE_LLM_STALL_BUDGET_S", "0")
    script = [("headers", "text/event-stream")]
    script += [("send", b": keep-alive\n\n"), ("sleep", 0.3)] * 30
    port = _serve(script)
    with pytest.raises(LLMStreamStalled) as ei:
        _call(port)
    # alive for 4x the 1 s bound (a slow prefill), then cut
    assert ei.value.phase == "first_token" and 4.0 <= ei.value.seconds < 6.0


def test_a_request_past_its_stall_budget_is_an_llm_issue(monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_FIRST_TOKEN_S", "1")
    monkeypatch.setenv("AIFORGE_LLM_STALL_BUDGET_S", "1.5")
    with request_health.bind(request_health.RequestHealth()):
        with pytest.raises(LLMStreamStalled):
            _call(_serve([("sleep", 2.5)]))          # 1 s of the 1.5 s budget
        with pytest.raises(LLMRequestFailing) as ei:
            _call(_serve([("sleep", 4.0)]))          # doubled bound: 2 s more
    msg = str(ei.value)
    assert "sent nothing back" in msg and "AIFORGE_LLM_STALL_BUDGET_S" in msg


def test_a_resend_after_a_stall_is_said_in_the_chat():
    seen = []
    ep = type("E", (), {"base_url": "http://box:1234/v1"})()
    with _wait_scope.status_sink(seen.append):
        _http_retry._say_retry(ep, 1, 3, "stream_stalled",
                               LLMStreamStalled("first_token", 180))
        _http_retry._say_retry(ep, 1, 3, "http_400", RuntimeError("x"))
    assert len(seen) == 1
    text = seen[0]["text"]
    assert "sent nothing back for 3m" in text and "try 2 of 3" in text
    assert "waiting up to 6m" in text


def test_model_connections_get_tcp_keepalive(monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_TCP_KEEPALIVE_S", "15")
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    ep = type("E", (), {"insecure_tls": False, "extras": {}})()
    conn, _ = _http._open_connection(ep, f"http://127.0.0.1:{srv.getsockname()[1]}/v1", 5)
    conn.connect()
    try:
        assert conn.sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) == 1
        if hasattr(socket, "TCP_KEEPIDLE"):
            assert conn.sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE) == 15
    finally:
        conn.close()
        srv.close()


def test_keepalive_off_switch(monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_TCP_KEEPALIVE_S", "0")
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    ep = type("E", (), {"insecure_tls": False, "extras": {}})()
    conn, _ = _http._open_connection(ep, f"http://127.0.0.1:{srv.getsockname()[1]}/v1", 5)
    conn.connect()
    try:
        assert conn.sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) == 0
    finally:
        conn.close()
        srv.close()


def test_a_restarted_call_is_not_reported_as_writing():
    from aiforge_core.runtime.chat_runs import _phase_after
    assert _phase_after({"type": "delta", "phase": "reset"},
                        "waiting for the model to answer") == "waiting for the model to answer"
    assert _phase_after({"type": "delta", "phase": "answer"}, "x") == "the model is writing"


def test_the_last_send_waits_only_what_is_left_of_the_budget(monkeypatch):
    import time
    monkeypatch.setenv("AIFORGE_LLM_FIRST_TOKEN_S", "1")
    monkeypatch.setenv("AIFORGE_LLM_STALL_BUDGET_S", "1.6")
    with request_health.bind(request_health.RequestHealth()):
        with pytest.raises(LLMStreamStalled):
            _call(_serve([("sleep", 2.5)]))          # 1 s spent
        t0 = time.monotonic()
        with pytest.raises(LLMRequestFailing):
            _call(_serve([("sleep", 4.0)]))          # bound 2 s, clamped to 1 s
        assert time.monotonic() - t0 < 1.8
