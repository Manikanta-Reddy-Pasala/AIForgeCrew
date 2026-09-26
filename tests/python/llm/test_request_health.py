"""Endpoint DOWN (wait forever) vs endpoint UP but THIS request keeps failing
(adapt, then an LLM issue) — llm/request_health, against real HTTP servers."""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from aiforge_core.llm import (
    call_meter,
    endpoint_breaker,
    model_outage,
    model_wait,
    request_health,
)


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_WAIT_MAX_S", "0")        # production: forever
    monkeypatch.delenv("AIFORGE_LLM_SAME_REQUEST_FAILS", raising=False)

    def _gaps(cap=None):
        while True:
            yield 0.02
    monkeypatch.setattr(model_wait, "delays", _gaps)
    model_wait._reset_for_tests()
    endpoint_breaker.reset()
    request_health._reset_for_tests()
    from aiforge_core.llm import _model_probe
    _model_probe._reset_for_tests()
    yield
    model_wait._reset_for_tests()
    endpoint_breaker.reset()
    request_health._reset_for_tests()


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Srv:
    """A fake OpenAI-compatible server. ``post(n)`` → (status, delay, sse) for
    the real request; ``probe(n)`` → (status, delay) for model_wait's
    one-token liveness completion (``max_tokens == 1``)."""

    def __init__(self, post, models=lambda: 200, probe=lambda n: (200, 0.0)):
        self.posts = 0
        self.probes = 0
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}/v1"
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                code = models()
                self._json(code, {"data": [{"id": "m"}]} if code == 200 else {})

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                try:
                    is_probe = json.loads(raw or b"{}").get("max_tokens") == 1
                except ValueError:
                    is_probe = False
                if is_probe:
                    srv.probes += 1
                    code, delay = probe(srv.probes)
                    sse = False
                else:
                    srv.posts += 1
                    code, delay, sse = post(srv.posts)
                time.sleep(delay)
                try:
                    if code != 200:
                        self._json(code, {"error": "upstream"})
                    elif sse:
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.end_headers()
                        chunk = {"choices": [{"delta": {"content": "hello"},
                                              "finish_reason": "stop"}]}
                        self.wfile.write(b"data: " + json.dumps(chunk).encode()
                                         + b"\n\ndata: [DONE]\n\n")
                    else:
                        self._json(200, {"choices": [{"message": {
                            "role": "assistant", "content": "hello"}}]})
                except OSError:
                    pass                     # the client gave up (a stall)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _post(url: str) -> str:
    req = urllib.request.Request(url + "/chat/completions", data=b"{}",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"]


def _stream(url: str) -> str:
    """One streamed send through the client's real transport + stream watch."""
    from aiforge_core.llm import client
    from aiforge_core.llm.client._http import _post_cancellable
    from aiforge_core.llm.router import Endpoint
    ep = Endpoint(provider="openai_compatible", base_url=url, model="m",
                  api_key="k", role="chat", extras={})
    client.set_delta_sink(lambda kind, text: None)
    payload = json.dumps({"model": "m", "messages": [
        {"role": "user", "content": "hi"}]}).encode()
    body = _post_cancellable(ep, payload, 30, threading.Event())
    return body["choices"][0]["message"]["content"]


# ── 1a: a slow prefill completes once the bound has doubled ─────────────────

def test_a_slow_prefill_completes_after_the_bound_doubles(monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_FIRST_TOKEN_S", "0.3")
    monkeypatch.setenv("AIFORGE_LLM_PREFILL_TOK_S", "1000000000")
    srv = _Srv(lambda n: (200, 0.5, True))        # first token after 0.5 s
    seen: list = []
    try:
        with model_wait.status_sink(seen.append):
            out = model_wait.call_with_wait(lambda: _stream(srv.url), url=srv.url)
    finally:
        srv.stop()
    assert out == "hello"
    assert srv.posts == 2              # stalled at 0.3 s, then 0.6 s > 0.5 s
    assert any(s["state"] == "resend" for s in seen)
    assert not any(s["state"] == "waiting" for s in seen)   # not an outage


def test_the_prefill_speed_seen_is_learned_per_endpoint(monkeypatch):
    from aiforge_core.llm.client._stream_health import first_token_s
    monkeypatch.setenv("AIFORGE_LLM_FIRST_TOKEN_S", "10")
    monkeypatch.setenv("AIFORGE_LLM_PREFILL_TOK_S", "200")
    payload = b"x" * 400_000                       # ~100k tokens
    before = first_token_s(payload, "http://a/v1")
    request_health.note_prefill("http://a/v1", 100_000, 1000.0)   # 100 tok/s
    assert first_token_s(payload, "http://a/v1") > before * 1.5
    assert first_token_s(payload, "http://b/v1") == before   # other endpoint


def test_a_hung_server_is_an_llm_issue_not_a_forever_wait(monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_FIRST_TOKEN_S", "0.05")
    monkeypatch.setenv("AIFORGE_LLM_SAME_REQUEST_FAILS", "3")
    srv = _Srv(lambda n: (200, 5.0, True))          # never answers in time
    try:
        with pytest.raises(model_outage.LLMRequestFailing) as ei:
            model_wait.call_with_wait(lambda: _stream(srv.url), url=srv.url)
    finally:
        srv.stop()
    assert srv.posts == 3                           # 0.05, 0.1, 0.2 s
    assert ei.value.reason == "llm_request_fails"
    assert model_outage.classify(ei.value) == model_outage.LLM_ISSUE


# ── 1b: an always-failing request surfaces after N ──────────────────────────

@pytest.mark.parametrize("n", [4, 2])
def test_an_always_504_request_is_an_llm_issue_after_n(monkeypatch, n):
    if n != 4:
        monkeypatch.setenv("AIFORGE_LLM_SAME_REQUEST_FAILS", str(n))
    srv = _Srv(lambda k: (504, 0.0, False))       # the proxy always times out
    try:
        with pytest.raises(model_outage.LLMRequestFailing):
            model_wait.call_with_wait(lambda: _post(srv.url), url=srv.url)
    finally:
        srv.stop()
    assert srv.posts == n


def test_sends_that_failed_while_up_spend_the_step_budget():
    tok = call_meter._STEP_CALLS.set({"n": 0})
    srv = _Srv(lambda k: (502, 0.0, False) if k <= 2 else (200, 0.0, False))

    def call():
        call_meter._STEP_CALLS.get()["n"] += 1
        return _post(srv.url)
    try:
        assert model_wait.call_with_wait(call, url=srv.url) == "hello"
        assert call_meter._STEP_CALLS.get()["n"] == 3      # no refund
    finally:
        srv.stop()
        call_meter._STEP_CALLS.reset(tok)


def test_sends_lost_to_an_outage_are_refunded(monkeypatch):
    tok = call_meter._STEP_CALLS.set({"n": 0})
    ups = iter([False, False, True])
    monkeypatch.setattr(model_wait, "probe", lambda *a, **k: next(ups))
    calls = []

    def call():
        call_meter._STEP_CALLS.get()["n"] += 1
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionRefusedError("refused")
        return "ok"
    try:
        assert model_wait.call_with_wait(call, url="http://m/v1") == "ok"
        assert call_meter._STEP_CALLS.get()["n"] == 1
    finally:
        call_meter._STEP_CALLS.reset(tok)


# ── 1c: a DOWN endpoint is still waited for, however long ───────────────────

def test_a_down_backend_behind_a_proxy_is_waited_for_not_counted():
    """502 on the send AND on the probe (the backend is down) is an outage:
    many more probes than N, then it comes back and the request goes through."""
    state = {"up": False, "probes": 0}

    def probe(n):
        state["probes"] += 1
        if state["probes"] > 12:
            state["up"] = True
        return (200 if state["up"] else 502), 0.0
    srv = _Srv(lambda k: (200, 0.0, False) if state["up"] else (502, 0.0, False),
               models=lambda: 502, probe=probe)
    try:
        assert model_wait.call_with_wait(lambda: _post(srv.url),
                                         url=srv.url) == "hello"
    finally:
        srv.stop()
    assert state["probes"] > request_health.same_request_fails()
    assert srv.posts == 2


def test_refused_forever_never_becomes_an_llm_issue(monkeypatch):
    import itertools
    ups = itertools.cycle([False] * 20 + [True])      # down 20 probes a time
    monkeypatch.setattr(model_wait, "probe", lambda *a, **k: next(ups))
    calls = []

    def call():
        calls.append(1)
        if len(calls) < 8:                            # > N outages in a row
            raise ConnectionRefusedError("refused")
        return "ok"
    assert model_wait.call_with_wait(call, url="http://m/v1") == "ok"


def test_busy_and_loading_never_count(monkeypatch):
    import io
    import urllib.error
    monkeypatch.setattr(model_wait, "probe", lambda *a, **k: True)
    monkeypatch.setattr(model_wait, "live_probe", lambda *a, **k: True)
    calls = []

    def call():
        calls.append(1)
        if len(calls) <= 8:
            raise urllib.error.HTTPError("http://m/v1", 503, "x", {},
                                         io.BytesIO(b'{"error": "loading"}'))
        return "ok"
    assert model_wait.call_with_wait(call, url="http://m/v1") == "ok"
    assert len(calls) == 9


# ── the ADK form (EscalatingLlm) counts too ─────────────────────────────────

def test_await_wait_raises_the_llm_issue(monkeypatch):
    import asyncio
    import io
    import urllib.error
    monkeypatch.setattr(model_wait, "probe", lambda *a, **k: True)
    monkeypatch.setattr(model_wait, "live_probe", lambda *a, **k: True)
    w = model_wait.Waiter("http://m/v1")
    exc = urllib.error.HTTPError("http://m/v1", 504, "x", {}, io.BytesIO(b"{}"))

    async def go():
        for _ in range(10):
            await w.await_wait(exc)
    with pytest.raises(model_outage.LLMRequestFailing):
        asyncio.run(go())
    assert w.health.fails == 4


# ── review r2: /models is not the model ─────────────────────────────────────

def _in_thread(fn):
    box: dict = {}

    def run():
        try:
            box["out"] = fn()
        except BaseException as exc:  # noqa: BLE001
            box["exc"] = exc
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, box


def test_a_router_answering_models_while_the_model_502s_is_waited_for():
    """nuc -> router -> dead MLX: /models answers 200, every completion
    (the probe too) 502s. Many more failures than N: still waiting, no
    LLMRequestFailing; the model comes back and the request goes through."""
    state = {"up": False}
    srv = _Srv(lambda k: (200, 0.0, False) if state["up"] else (502, 0.0, False),
               models=lambda: 200,
               probe=lambda n: (200 if state["up"] else 502, 0.0))
    try:
        t, box = _in_thread(lambda: model_wait.call_with_wait(
            lambda: _post(srv.url), url=srv.url, model="m"))
        deadline = time.monotonic() + 10
        while srv.probes < 5 * request_health.same_request_fails() \
                and time.monotonic() < deadline:
            time.sleep(0.02)
        assert t.is_alive() and "exc" not in box, box
        assert srv.probes >= 5 * request_health.same_request_fails()
        state["up"] = True
        t.join(10)
    finally:
        srv.stop()
    assert box.get("out") == "hello", box


def test_a_busy_queue_that_holds_the_probe_too_is_waited_for(monkeypatch):
    """A box whose queue holds every request (llama.cpp/vLLM/MLX queue
    silently): the big request stalls, the tiny probe does not answer
    promptly either — busy, not an LLM issue. Waits, then completes."""
    monkeypatch.setenv("AIFORGE_LLM_FIRST_TOKEN_S", "0.05")
    monkeypatch.setenv("AIFORGE_LLM_LIVE_PROBE_S", "0.5")
    monkeypatch.setenv("AIFORGE_LLM_SAME_REQUEST_FAILS", "2")
    state = {"busy": True}

    def queued():                    # held in the queue while the box is busy
        end = time.monotonic() + 3.0
        while state["busy"] and time.monotonic() < end:
            time.sleep(0.02)
    srv = _Srv(lambda k: (queued(), (200, 0.0, True))[1],
               probe=lambda n: (queued(), (200, 0.0))[1])
    try:
        t, box = _in_thread(lambda: model_wait.call_with_wait(
            lambda: _stream(srv.url), url=srv.url, model="m"))
        deadline = time.monotonic() + 15
        while srv.probes < 4 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert t.is_alive() and "exc" not in box, box
        state["busy"] = False
        t.join(60)
    finally:
        srv.stop()
    assert box.get("out") == "hello", box


def test_the_model_answers_the_probe_but_always_504s_this_request():
    """The tiny probe succeeds promptly, the big request always 504s: an LLM
    issue after N — exactly N sends."""
    srv = _Srv(lambda k: (504, 0.0, False), models=lambda: 502)
    try:
        with pytest.raises(model_outage.LLMRequestFailing):
            model_wait.call_with_wait(lambda: _post(srv.url), url=srv.url,
                                      model="m")
    finally:
        srv.stop()
    assert srv.posts == request_health.same_request_fails()
    assert srv.probes == srv.posts


def test_a_doubled_first_token_bound_outlives_the_read_timeout(monkeypatch):
    from aiforge_core.llm.client._stream_health import StreamWatch
    monkeypatch.setenv("AIFORGE_LLM_FIRST_TOKEN_S", "100")
    monkeypatch.setenv("AIFORGE_LLM_PREFILL_TOK_S", "0")
    assert StreamWatch(None, b"{}", 150)._tightest(200) == 0.0   # first send
    h = request_health.RequestHealth()
    h.stalls = 1
    with request_health.bind(h):
        w = StreamWatch(None, b"{}", 150)
        assert w._first == 200 and w._tightest(w._first) == 200
