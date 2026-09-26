"""A streamed call is health-bounded: silence before the first token, or
between chunks, past its bound is a retryable LLMStreamStalled — while ANY
byte (a token, an SSE keep-alive comment) counts as the server being alive.

Driven against a real local SSE server over a real socket, because the bounds
are socket timeouts and a stub response would never exercise them."""
import http.client
import json
import socket
import threading
import time

import pytest

from aiforge_core.llm.client import _errors, _http
from aiforge_core.llm.client._stream_health import (
    LLMStreamStalled,
    StreamWatch,
    first_token_s,
)


def _chunk(text):
    return json.dumps({"id": "c", "choices": [{"index": 0,
                                               "delta": {"content": text}}]})


def _serve(script):
    """One-shot HTTP server: reads the request, then plays ``script`` — a list
    of ("sleep", s) / ("send", bytes) / ("headers", ctype) steps."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def _run():
        c, _ = srv.accept()
        try:
            buf = b""
            while b"\r\n\r\n" not in buf:
                buf += c.recv(65536)
            head, body = buf.split(b"\r\n\r\n", 1)
            n = int([ln.split(b":")[1] for ln in head.split(b"\r\n")
                     if ln.lower().startswith(b"content-length")][0])
            while len(body) < n:
                body += c.recv(65536)
            for kind, arg in script:
                if kind == "sleep":
                    time.sleep(arg)
                elif kind == "headers":
                    c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: " + arg.encode()
                              + b"\r\nConnection: close\r\n\r\n")
                else:
                    c.sendall(arg)
        except OSError:
            pass
        finally:
            time.sleep(0.2)
            c.close()
            srv.close()

    threading.Thread(target=_run, daemon=True).start()
    return srv.getsockname()[1]


def _call(port, read_timeout=30.0, payload=b'{"x":1}'):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=read_timeout)
    conn.request("POST", "/v1/chat/completions", body=payload,
                 headers={"Content-Type": "application/json"})
    got = []
    try:
        return _http._read_sse_response(
            conn, "u", lambda k, t: got.append((k, t)), payload=payload,
            read_timeout=read_timeout), got
    finally:
        conn.close()


def _sse(text):
    return f"data: {text}\n\n".encode()


@pytest.fixture
def fast_bounds(monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_FIRST_TOKEN_S", "0.6")
    monkeypatch.setenv("AIFORGE_LLM_STREAM_IDLE_S", "0.6")
    monkeypatch.setenv("AIFORGE_LLM_PREFILL_TOK_S", "1000000")


def test_a_server_that_never_sends_a_first_token_is_a_stall(fast_bounds):
    port = _serve([("headers", "text/event-stream"), ("sleep", 3)])
    t0 = time.monotonic()
    with pytest.raises(LLMStreamStalled) as ei:
        _call(port)
    assert ei.value.phase == "first_token"
    assert time.monotonic() - t0 < 2.5


def test_silence_before_the_status_line_is_a_first_token_stall(fast_bounds):
    port = _serve([("sleep", 3)])
    with pytest.raises(LLMStreamStalled) as ei:
        _call(port)
    assert ei.value.phase == "first_token"


def test_a_stream_that_goes_idle_mid_answer_is_a_stall(fast_bounds):
    port = _serve([("headers", "text/event-stream"), ("send", _sse(_chunk("a"))),
                   ("sleep", 3)])
    with pytest.raises(LLMStreamStalled) as ei:
        _call(port)
    assert ei.value.phase == "idle"


def test_keep_alives_count_as_liveness(fast_bounds):
    """A slow prefill that sends SSE comments is alive — never cut off, even
    though no token arrives for longer than the bound."""
    steps = [("headers", "text/event-stream")]
    for _ in range(5):
        steps += [("sleep", 0.3), ("send", b": keep-alive\n\n")]
    steps += [("send", _sse(_chunk("hi"))),
              ("send", _sse(json.dumps({"choices": [{"index": 0, "delta": {},
                                                     "finish_reason": "stop"}]}))),
              ("send", _sse("[DONE]"))]
    body, got = _call(_serve(steps))
    assert body["choices"][0]["message"]["content"] == "hi"
    assert ("content", "hi") in got


def test_a_slow_but_steady_stream_runs_unbounded(fast_bounds):
    steps = [("headers", "text/event-stream")]
    for i in range(6):                           # 6 x 0.4s > the 0.6s bound
        steps += [("sleep", 0.4), ("send", _sse(_chunk(str(i))))]
    steps += [("send", _sse("[DONE]"))]
    body, _ = _call(_serve(steps))
    assert body["choices"][0]["message"]["content"] == "012345"


def test_a_server_that_ignored_stream_gets_the_full_read_timeout(fast_bounds):
    """Plain JSON arrives only after the whole generation: silence there is
    normal, so the health bounds step aside."""
    doc = json.dumps({"choices": [{"index": 0, "message": {
        "role": "assistant", "content": "late"}}]}).encode()
    port = _serve([("sleep", 0.2),
                   ("send", b"HTTP/1.1 200 OK\r\nContent-Type: application/json"
                            b"\r\nContent-Length: " + str(len(doc)).encode()
                            + b"\r\n\r\n"),
                   ("sleep", 1.0), ("send", doc)])
    body, _ = _call(port)
    assert body["choices"][0]["message"]["content"] == "late"


def test_the_callers_shorter_timeout_keeps_its_meaning(fast_bounds, monkeypatch):
    """A 0.3s caller timeout is tighter than the health bound, so it fires as
    the plain read timeout it always was (shipped, not re-POSTed)."""
    port = _serve([("headers", "text/event-stream"), ("sleep", 2)])
    with pytest.raises(TimeoutError) as ei:
        _call(port, read_timeout=0.3)
    assert not isinstance(ei.value, LLMStreamStalled)


def test_the_first_token_bound_scales_with_the_prompt(monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_FIRST_TOKEN_S", "180")
    monkeypatch.setenv("AIFORGE_LLM_PREFILL_TOK_S", "200")
    assert first_token_s(b"x" * 400) == 180.0
    # ~100k tokens at 200 tok/s of prefill = 500s
    assert first_token_s(b"x" * 400_000) == pytest.approx(500.0)
    monkeypatch.setenv("AIFORGE_LLM_FIRST_TOKEN_S", "0")
    assert first_token_s(b"x" * 400_000) == 0.0


def test_zero_disables_and_no_socket_is_a_no_op(monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_FIRST_TOKEN_S", "0")
    monkeypatch.setenv("AIFORGE_LLM_STREAM_IDLE_S", "0")
    w = StreamWatch(None, b"{}", 900)
    w.arm_first_token()
    w.got_data()
    exc = TimeoutError("timed out")
    assert w.stalled(exc) is exc


def test_a_stall_is_retryable_and_labelled():
    retry, label = _errors._is_transient_exc(LLMStreamStalled("idle", 120))
    assert retry is True and label == "stream_stalled"


def test_a_stall_does_not_trip_the_connect_breaker():
    from aiforge_core.llm import endpoint_breaker
    assert endpoint_breaker.is_connect_error(
        LLMStreamStalled("first_token", 180)) is False


def test_the_escalation_policy_treats_a_stall_as_transient():
    from aiforge_core.runtime.escalating_llm._policy import _is_transient_llm_error
    assert _is_transient_llm_error(LLMStreamStalled("idle", 120))


def test_the_retry_wrapper_re_posts_a_stalled_stream(monkeypatch):
    """Unlike a shipped read timeout, a stall is retried: the closed
    connection already aborted the stuck generation."""
    from aiforge_core.llm.client import _http_retry
    from aiforge_core.llm.types import Endpoint
    calls = []

    def _post(ep, payload, timeout_s, **kw):
        calls.append(1)
        if len(calls) == 1:
            kw["sent"][0] = True
            raise LLMStreamStalled("first_token", 180)
        return {"ok": True}

    monkeypatch.setattr(_http, "_post", _post)
    monkeypatch.setattr(_http_retry.time, "sleep", lambda s: None)
    monkeypatch.setattr(_http_retry, "_endpoint_down", lambda ep: False)
    ep = Endpoint(provider="openai_compat", base_url="http://127.0.0.1:9/v1",
                  model="m", api_key="", role="chat", extras={})
    assert _http._post_with_retry(ep, b"{}", 900, role="chat",
                                  source="test") == {"ok": True}
    assert len(calls) == 2
