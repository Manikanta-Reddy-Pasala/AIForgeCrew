"""llm/model_wait + llm/model_outage: wait for a model that is down, never
give up on it, stop on a cancel, fail fast on a configuration error."""
from __future__ import annotations

import io
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from aiforge_core.llm import endpoint_breaker, model_outage, model_wait
from aiforge_core.llm.client._errors import _LLMCancelled, _ModelReloading
from aiforge_core.llm.client._stream_health import LLMStreamStalled


@pytest.fixture(autouse=True)
def _unbounded_fast(monkeypatch):
    """The production setting (wait forever) with millisecond probe gaps."""
    monkeypatch.setenv("AIFORGE_LLM_WAIT_MAX_S", "0")

    def _fast(cap=None):
        while True:
            yield 0.02
    monkeypatch.setattr(model_wait, "delays", _fast)
    model_wait._reset_for_tests()
    endpoint_breaker.reset()
    yield
    model_wait._reset_for_tests()
    endpoint_breaker.reset()


def _http(code, body=b"{}"):
    return urllib.error.HTTPError("http://m/v1", code, "x", {}, io.BytesIO(body))


# ── classification ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("exc", [
    ConnectionRefusedError("refused"),
    urllib.error.URLError(ConnectionRefusedError(61, "Connection refused")),
    socket.gaierror(8, "nodename nor servname provided, or not known"),
    ConnectionResetError(54, "Connection reset by peer"),
    ConnectionError("LLM endpoint unreachable (h:1): h:1 failed to connect "
                    "2 time(s) in a row; skipping it for another 30s"),
    LLMStreamStalled("first_token", 180),
    _ModelReloading("model unavailable (reloading?): model unloaded"),
    TimeoutError("timed out"),                      # never reached the model
    _http(502), _http(503), _http(504), _http(408),
    _http(429, b'{"error": {"message": "Rate limit reached, slow down"}}'),
    _http(400, b'{"error": "No models loaded. Please load a model"}'),
    _http(404, b'{"error": {"message": "Model is loading, try again"}}'),
])
def test_outages(exc):
    assert model_outage.classify(exc) == model_outage.OUTAGE


@pytest.mark.parametrize("exc", [
    _http(401), _http(403), _http(400, b'{"error": "bad request shape"}'),
    _http(404, b'{"error": {"message": "model not found: foo"}}'),
    _http(429, b'{"error": {"code": "insufficient_quota"}}'),
])
def test_config_errors_are_not_outages(exc):
    assert model_outage.classify(exc) == model_outage.CONFIG


def test_other_verdicts():
    from aiforge_core.llm.client._http_stream import TIMEOUT_SHIPPED_ATTR
    shipped = TimeoutError("read timed out")
    setattr(shipped, TIMEOUT_SHIPPED_ATTR, True)
    assert model_outage.classify(shipped) == model_outage.SHIPPED
    assert model_outage.classify(_LLMCancelled("stop")) == model_outage.CANCELLED
    assert model_outage.classify(ValueError("bad json")) == model_outage.OTHER
    assert model_outage.classify(_http(500)) == model_outage.OTHER


def test_litellm_connection_error_counts_only_when_it_names_a_connect():
    class APIConnectionError(Exception):
        pass
    assert model_outage.is_outage(APIConnectionError(
        "Cannot connect to host 127.0.0.1:1234 [Connection refused]"))
    assert model_outage.classify(APIConnectionError(
        "JSONDecodeError: Expecting value")) == model_outage.OTHER


def test_the_clients_own_verdicts():
    """The exhausted chain carries its transport error; a model the endpoint
    does not serve is config, unless the endpoint serves nothing (unloaded)."""
    from aiforge_core.llm.client import _exhausted_error, _model_missing_error
    from aiforge_core.llm.router import Endpoint
    ep = Endpoint(provider="openai_compatible", base_url="http://127.0.0.1:9/v1",
                  model="m", api_key="", role="chat", extras={})
    dead = _exhausted_error("chat", ep, None, None, 0,
                            {"exc": ConnectionRefusedError("refused")})
    assert model_outage.is_outage(dead)
    assert model_outage.classify(_model_missing_error("chat", ep, ["other"])) \
        == model_outage.CONFIG
    assert model_outage.is_outage(_model_missing_error("chat", ep, []))


# ── the primitive ───────────────────────────────────────────────────────────

def _flaky(fails, exc_factory=lambda: ConnectionRefusedError("refused")):
    calls = []

    def _call():
        calls.append(1)
        if len(calls) <= fails:
            raise exc_factory()
        return "ok"
    return _call, calls


def test_waits_until_the_endpoint_answers_then_resends(monkeypatch):
    ups = iter([False, False, True] * 100)
    monkeypatch.setattr(model_wait, "probe", lambda u, k="", timeout_s=5.0: next(ups))
    call, calls = _flaky(3)
    assert model_wait.call_with_wait(call, url="http://m/v1") == "ok"
    assert len(calls) == 4


def test_config_error_fails_fast_without_probing(monkeypatch):
    probed = []
    monkeypatch.setattr(model_wait, "probe",
                        lambda *a, **k: probed.append(1) or True)
    call, calls = _flaky(5, lambda: _http(401))
    with pytest.raises(urllib.error.HTTPError):
        model_wait.call_with_wait(call, url="http://m/v1")
    assert len(calls) == 1 and not probed


def test_a_bound_gives_up_and_negative_disables(monkeypatch):
    monkeypatch.setattr(model_wait, "probe", lambda *a, **k: False)
    monkeypatch.setenv("AIFORGE_LLM_WAIT_MAX_S", "0.1")
    call, _ = _flaky(10_000)
    with pytest.raises(ConnectionRefusedError):
        model_wait.call_with_wait(call, url="http://m/v1")
    monkeypatch.setenv("AIFORGE_LLM_WAIT_MAX_S", "-1")
    call, calls = _flaky(10_000)
    with pytest.raises(ConnectionRefusedError):
        model_wait.call_with_wait(call, url="http://m/v1")
    assert len(calls) == 1


def test_optional_calls_never_wait(monkeypatch):
    monkeypatch.setattr(model_wait, "probe", lambda *a, **k: True)
    call, calls = _flaky(3)
    with model_wait.optional(), pytest.raises(ConnectionRefusedError):
        model_wait.call_with_wait(call, url="http://m/v1")
    assert len(calls) == 1


@pytest.mark.parametrize("how", ["scope", "client_cancel", "shutdown"])
def test_cancel_while_waiting(monkeypatch, how):
    monkeypatch.setattr(model_wait, "probe", lambda *a, **k: False)
    ev = threading.Event()
    box: dict = {}

    def _run():
        from aiforge_core.llm import client
        try:
            if how == "client_cancel":
                client.set_cancel_event(ev)
            with model_wait.scope(ev if how == "scope" else None, "ticket lost"):
                model_wait.call_with_wait(_flaky(10 ** 9)[0], url="http://m/v1")
        except BaseException as exc:  # noqa: BLE001
            box["exc"] = exc

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    time.sleep(0.2)
    assert t.is_alive()                      # still waiting, not failed
    if how == "shutdown":
        model_wait.shutdown()
    else:
        ev.set()
    t.join(5)
    assert not t.is_alive()
    assert isinstance(box["exc"], model_wait.ModelWaitCancelled)
    assert isinstance(box["exc"], _LLMCancelled)     # every layer's "cancelled"


def test_status_changes_are_reported_not_every_probe(monkeypatch):
    monkeypatch.setattr(model_wait, "delays",
                        lambda cap=None: iter([0.01, 0.01, 0.02] + [0.03] * 50))
    ups = iter([False] * 20 + [True])
    monkeypatch.setattr(model_wait, "probe", lambda *a, **k: next(ups))
    seen: list = []
    call, _ = _flaky(1)
    with model_wait.status_sink(seen.append):
        assert model_wait.call_with_wait(call, url="http://m/v1") == "ok"
    states = [s["state"] for s in seen]
    assert states[0] == "waiting" and states[-1] == "back"
    assert states.count("waiting") == 3          # one per gap change, 21 probes
    assert "waiting for model at http://m/v1 (down" in seen[0]["text"]
    assert "next probe" in seen[0]["text"]


def test_outage_sends_do_not_spend_the_step_budget(monkeypatch):
    from aiforge_core.llm import call_meter
    monkeypatch.setattr(model_wait, "probe", lambda *a, **k: True)
    counter = call_meter.step_begin()
    tok = call_meter.step_bind(counter)
    try:
        fails = [5]

        def _call():
            counter["n"] += 1                    # what the transport does
            if fails[0] > 0:
                fails[0] -= 1
                raise ConnectionRefusedError("refused")
            return "ok"
        counter["n"] = 44
        assert model_wait.call_with_wait(_call, url="http://m/v1") == "ok"
        assert counter["n"] == 45                # only the send that answered
    finally:
        call_meter.step_reset(tok)


def test_wrong_model_id_is_not_waited_for_even_if_it_says_not_loaded(monkeypatch):
    from aiforge_core.llm.client import _models
    monkeypatch.setattr(_models, "model_is_missing",
                        lambda url, model, key="": ["other-model"])
    call, calls = _flaky(3, lambda: _ModelReloading("No models loaded"))
    with pytest.raises(_ModelReloading):
        model_wait.call_with_wait(call, url="http://m/v1", model="mine")
    assert len(calls) == 1


# ── against a real (fake) HTTP server: refused → 503 → 200 ─────────────────

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Server:
    """Not listening at first; later up, answering chat with 503 twice, then
    200. ``/v1/models`` always answers once up."""

    def __init__(self, port: int, busy: int = 2) -> None:
        self.port, self.busy, self.posts = port, busy, 0
        self.httpd = None

    def start(self) -> None:
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: D401 — silence
                pass

            def _send(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._send(200, {"data": [{"id": "m"}]})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(n)
                srv.posts += 1
                if srv.posts <= srv.busy:
                    self._send(503, {"error": "Service Unavailable"})
                else:
                    self._send(200, {"choices": [{"message": {
                        "role": "assistant", "content": "hello"}}]})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()


def _post(url: str) -> str:
    req = urllib.request.Request(url + "/chat/completions", data=b"{}",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=2) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"]


def test_refused_then_503_then_200_against_a_real_server():
    port = _free_port()
    url = f"http://127.0.0.1:{port}/v1"
    srv = _Server(port)
    timer = threading.Timer(0.3, srv.start)
    timer.start()
    seen: list = []
    try:
        with model_wait.status_sink(seen.append):
            out = model_wait.call_with_wait(lambda: _post(url), url=url)
    finally:
        timer.cancel()
        srv.stop()
    assert out == "hello"
    assert srv.posts == 3                        # 503, 503, 200
    assert seen[-1]["state"] == "back"


def test_client_complete_waits_for_a_real_server(monkeypatch):
    """The whole client path: preflight refused → breaker open → 503 → 200."""
    from aiforge_core.llm import client
    from aiforge_core.llm.router import Endpoint
    port = _free_port()
    url = f"http://127.0.0.1:{port}/v1"
    ep = Endpoint(provider="openai_compatible", base_url=url, model="m",
                  api_key="k", role="chat", extras={})
    monkeypatch.setattr(client, "resolve", lambda role: ep)
    monkeypatch.setattr(client, "fallback", lambda role: None)
    monkeypatch.setattr(client, "escalate", lambda *a, **k: None)
    monkeypatch.setattr(client, "_try_model_chain", lambda *a, **k: None)
    monkeypatch.setenv("AIFORGE_LLM_RETRY_MAX", "1")
    monkeypatch.setenv("AIFORGE_LLM_EMPTY_RETRIES", "0")
    monkeypatch.setenv("AIFORGE_CHAT_STREAM", "0")
    srv = _Server(port)
    timer = threading.Timer(0.4, srv.start)
    timer.start()
    try:
        out = client.complete("chat", [{"role": "user", "content": "hi"}])
    finally:
        timer.cancel()
        srv.stop()
    assert out == "hello"


# ── ticket lease: the claim keeps beating while the run waits ───────────────

def test_ticket_lease_heartbeats_during_the_wait(monkeypatch):
    from aiforge_core.tickets import lease, store
    beats, events = [], []
    monkeypatch.setattr(store, "renew_claim",
                        lambda tid: beats.append(time.monotonic()) or True)
    monkeypatch.setattr(store, "add_event",
                        lambda tid, role, kind, body, md=None: events.append(kind))
    monkeypatch.setattr(lease, "_stop_jobs", lambda owner: None)
    ups = iter([False] * 25 + [True])
    monkeypatch.setattr(model_wait, "probe", lambda *a, **k: next(ups))
    with lease.hold_claim(7, interval_s=0.05):
        out = model_wait.call_with_wait(_flaky(1)[0], url="http://m/v1")
    assert out == "ok"
    assert len(beats) >= 3                        # ~0.5 s of waiting
    assert "llm_wait" in events


def test_a_lost_claim_ends_the_wait(monkeypatch):
    from aiforge_core.tickets import lease, store
    monkeypatch.setattr(store, "renew_claim", lambda tid: False)  # cancelled
    monkeypatch.setattr(store, "add_event", lambda *a, **k: None)
    monkeypatch.setattr(lease, "_stop_jobs", lambda owner: None)
    monkeypatch.setattr(model_wait, "probe", lambda *a, **k: False)
    with pytest.raises(model_wait.ModelWaitCancelled) as ei:
        with lease.hold_claim(7, interval_s=0.05):
            model_wait.call_with_wait(_flaky(10 ** 9)[0], url="http://m/v1")
    assert "claim lost" in ei.value.reason
