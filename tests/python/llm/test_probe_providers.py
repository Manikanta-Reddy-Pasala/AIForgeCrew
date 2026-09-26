"""The liveness probe goes the request's own way, is valid for every provider,
falls back to counting on a client-error probe, and a request that CRASHES
the model server ends as an LLM issue instead of being re-sent forever.
Against real (fake) HTTP servers."""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from aiforge_core.llm import (
    _model_probe,
    endpoint_breaker,
    model_outage,
    model_wait,
    request_health,
)


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_WAIT_MAX_S", "0")        # forever
    monkeypatch.delenv("AIFORGE_LLM_SAME_REQUEST_FAILS", raising=False)
    monkeypatch.delenv("AIFORGE_LLM_CRASH_RESENDS", raising=False)

    def _gaps(cap=None):
        while True:
            yield 0.05
    monkeypatch.setattr(model_wait, "delays", _gaps)
    model_wait._reset_for_tests()
    endpoint_breaker.reset()
    request_health._reset_for_tests()
    _model_probe._reset_for_tests()
    yield
    model_wait._reset_for_tests()
    endpoint_breaker.reset()
    _model_probe._reset_for_tests()


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Raw:
    """A raw-socket HTTP server. ``answer(body) -> (status, json) | "crash"``;
    "crash" drops the connection and takes the whole server down for
    ``down_s`` (a model server OOM-restarting on that prompt)."""

    def __init__(self, answer, down_s: float = 0.6) -> None:
        self.answer, self.down_s = answer, down_s
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}/v1"
        self.bodies: list[dict] = []
        self.posts = self.probes = self.crashes = 0
        self._stop = False
        self._lsock = None
        self._up = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()
        self._up.wait(5)

    def _listen(self):
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", self.port))
        s.listen(16)
        s.settimeout(0.2)
        return s

    def _serve(self):
        self._lsock = self._listen()
        self._up.set()
        while not self._stop:
            try:
                conn, _ = self._lsock.accept()
            except (socket.timeout, OSError):
                continue
            try:
                crashed = self._one(conn)
            except OSError:
                crashed = False
            if crashed:
                self._lsock.close()          # nothing listens: refused
                conn.close()
                time.sleep(self.down_s)
                if not self._stop:
                    self._lsock = self._listen()

    def _one(self, conn) -> bool:
        conn.settimeout(5)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(65536)
            if not chunk:
                return False
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        length = 0
        for line in head.split(b"\r\n")[1:]:
            k, _, v = line.partition(b":")
            if k.strip().lower() == b"content-length":
                length = int(v.strip())
        while len(rest) < length:
            rest += conn.recv(65536)
        try:
            body = json.loads(rest or b"{}")
        except ValueError:
            body = {}
        self.bodies.append(body)
        is_probe = body.get("max_tokens") == 1 \
            or body.get("max_completion_tokens") == 1
        if is_probe:
            self.probes += 1
        else:
            self.posts += 1
        got = self.answer(body, is_probe)
        if isinstance(got, tuple) and got and got[0] == "crash":
            time.sleep(got[1])               # generating, then dies
            got = "crash"
        if got == "crash":
            self.crashes += 1
            return True
        status, obj = got
        raw = json.dumps(obj).encode()
        conn.sendall(f"HTTP/1.1 {status} X\r\nContent-Type: application/json"
                     f"\r\nContent-Length: {len(raw)}\r\nConnection: close"
                     f"\r\n\r\n".encode() + raw)
        conn.close()
        return False

    def stop(self):
        self._stop = True
        try:
            self._lsock.close()
        except OSError:
            pass


_OK = (200, {"choices": [{"message": {"role": "assistant", "content": "hi"}}]})


def _post(url: str) -> str:
    req = urllib.request.Request(url + "/chat/completions",
                                 data=json.dumps({"model": "m"}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"]


def _bounded(fn, limit_s: float = 30.0):
    """Run ``fn`` on a thread; a forever-retry fails the test, not the run."""
    box: dict = {}

    def run():
        try:
            box["out"] = fn()
        except BaseException as exc:  # noqa: BLE001
            box["exc"] = exc
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(limit_s)
    assert not t.is_alive(), "still re-sending: the LLM-issue stop never fired"
    return box


# ── the probe is valid for the provider ────────────────────────────────────

def test_a_401_probe_with_a_504_request_is_ambiguous_so_it_waits():
    """The probe is refused with an auth error but the request failed
    differently: nothing CLEAR says this request is the problem — wait (and
    go through once the server serves again), never stop."""
    t0 = time.monotonic()

    def answer(body, probe):
        if time.monotonic() - t0 > 1.5:
            return _OK
        return (401, {"error": "invalid x-api-key"}) if probe \
            else (504, {"error": "gateway"})
    srv = _Raw(answer)
    try:
        box = _bounded(lambda: model_wait.call_with_wait(
            lambda: _post(srv.url), url=srv.url, model="m"))
    finally:
        srv.stop()
    assert box.get("out") == "hi", box


def test_a_router_mid_swap_404_model_loading_is_waited_for():
    """llama-swap / Ollama answer 404/400 "model not found / loading" while
    swapping: BUSY — no counting, no resend until it serves."""
    t0 = time.monotonic()

    def answer(body, probe):
        if time.monotonic() - t0 > 1.5:
            return _OK
        if probe:
            return 404, {"error": "model 'm' not found, loading"}
        return 504, {"error": "gateway"}
    srv = _Raw(answer)
    try:
        box = _bounded(lambda: model_wait.call_with_wait(
            lambda: _post(srv.url), url=srv.url, model="m"))
    finally:
        srv.stop()
    assert box.get("out") == "hi", box
    assert srv.posts <= 3          # no resend storm while it swapped


def test_only_the_same_client_error_on_both_is_clear():
    from aiforge_core.llm import _probe_states as ps
    assert ps.same_client_error(401, {403})
    assert ps.same_client_error(404, {404})
    assert not ps.same_client_error(401, {504})
    assert not ps.same_client_error(0, {401})
    assert ps.status_state(400, "model is loading") == ps.BUSY
    assert ps.status_state(401, "invalid api key") == ps.INCONCLUSIVE


def test_a_reasoning_model_gets_its_own_token_parameter():
    def answer(body, probe):
        if "temperature" in body:
            return 400, {"error": {"message": "Unsupported parameter: "
                                              "'temperature'"}}
        if "max_tokens" in body:
            return 400, {"error": {"message": "Unsupported parameter: "
                                              "'max_tokens' is not supported "
                                              "with this model. Use "
                                              "'max_completion_tokens'."}}
        return _OK if probe else (504, {"error": "gateway"})
    srv = _Raw(answer)
    try:
        assert _model_probe.completion_probe(srv.url, "k", "m") == \
            _model_probe.OK
        box = _bounded(lambda: model_wait.call_with_wait(
            lambda: _post(srv.url), url=srv.url, model="m"))
    finally:
        srv.stop()
    assert isinstance(box.get("exc"), model_outage.LLMRequestFailing)
    assert any(b.get("max_completion_tokens") == 1 for b in srv.bodies)
    assert not any("temperature" in b for b in srv.bodies)


def test_a_provider_model_is_probed_through_litellm(monkeypatch):
    import litellm
    calls = []

    class _Auth(Exception):
        status_code = 401

    def fake(**kw):
        calls.append(kw)
        if kw["model"].endswith("bad"):
            raise _Auth("invalid x-api-key")
        return {"choices": [{"message": {"content": "."}}]}
    monkeypatch.setattr(litellm, "completion", fake)
    assert _model_probe.completion_probe("", "k", "anthropic/some-model") == \
        _model_probe.OK
    assert calls[0]["model"] == "anthropic/some-model"
    assert calls[0]["max_tokens"] == 1 and calls[0]["drop_params"] is True
    assert "temperature" not in calls[0]
    assert _model_probe.completion_probe("https://x", "k", "azure/bad") == \
        _model_probe.INCONCLUSIVE
    calls.clear()
    # The request's own kwargs (self-signed proxy, gateway headers, version).
    _model_probe.register_send("https://gw/v1", "azure/dep", {
        "ssl_verify": False, "extra_headers": {"X-Gw": "1"},
        "api_version": "2024-06-01", "drop_params": True, "timeout": 900})
    assert _model_probe.completion_probe("https://gw/v1", "k", "azure/dep") \
        == _model_probe.OK
    sent = calls[-1]
    assert sent["ssl_verify"] is False and sent["extra_headers"] == {"X-Gw": "1"}
    assert sent["api_version"] == "2024-06-01" and sent["max_tokens"] == 1
    calls.clear()
    # The OpenAI wire (a local server) stays plain HTTP.
    _model_probe.completion_probe("http://127.0.0.1:9/v1", "", "openai/m",
                                  timeout_s=1)
    assert calls == []


def test_the_probe_states():
    assert _model_probe._exc_state(urllib.error.URLError(
        ConnectionRefusedError("refused"))) == _model_probe.REFUSED
    assert _model_probe._exc_state(urllib.error.URLError(socket.gaierror(
        8, "nodename nor servname provided"))) == _model_probe.DOWN
    assert _model_probe._exc_state(urllib.error.URLError(
        socket.timeout("timed out"))) == _model_probe.BUSY
    assert _model_probe._status_state(502) == _model_probe.DOWN
    assert _model_probe._status_state(503) == _model_probe.BUSY
    assert _model_probe._status_state(404) == _model_probe.INCONCLUSIVE
    # the recovery probe: a client-error answer is a live server
    srv = _Raw(lambda body, probe: (404, {"error": "no route"}))
    try:
        assert model_wait.probe(srv.url, "", model="m") is True
    finally:
        srv.stop()


def test_a_recent_success_is_reused_not_reprobed():
    srv = _Raw(lambda body, probe: _OK)
    try:
        assert _model_probe.live_state(srv.url, "", "m") == _model_probe.OK
        assert _model_probe.live_state(srv.url, "", "m") == _model_probe.OK
        assert srv.probes == 1
        _model_probe.live_state(srv.url, "", "m", fresh=True)
        assert srv.probes == 2
        _model_probe.live_state(srv.url, "other-key", "m")   # not reused
        assert srv.probes == 3
    finally:
        srv.stop()


# ── a request that crashes the server ──────────────────────────────────────

def test_a_request_that_crashes_the_server_is_an_llm_issue():
    srv = _Raw(lambda body, probe: _OK if probe else "crash")
    try:
        box = _bounded(lambda: model_wait.call_with_wait(
            lambda: _post(srv.url), url=srv.url, model="m"), 60)
    finally:
        srv.stop()
    exc = box.get("exc")
    assert isinstance(exc, model_outage.LLMRequestFailing), box
    assert "crashes the model server" in str(exc)
    assert model_outage.classify(exc) == model_outage.LLM_ISSUE
    # the first crash is not known to be ours; then K resends crash it
    assert srv.crashes == 1 + request_health.crash_resends()


def test_the_crash_count_is_a_knob(monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_CRASH_RESENDS", "2")
    srv = _Raw(lambda body, probe: _OK if probe else "crash", down_s=0.3)
    try:
        box = _bounded(lambda: model_wait.call_with_wait(
            lambda: _post(srv.url), url=srv.url, model="m"), 60)
    finally:
        srv.stop()
    assert isinstance(box.get("exc"), model_outage.LLMRequestFailing)
    assert srv.crashes == 3


def test_a_server_that_dies_seconds_after_this_request_is_an_llm_issue():
    srv = _Raw(lambda body, probe: _OK if probe else ("crash", 1.0),
               down_s=0.4)
    try:
        box = _bounded(lambda: model_wait.call_with_wait(
            lambda: _post(srv.url), url=srv.url, model="m"), 60)
    finally:
        srv.stop()
    assert isinstance(box.get("exc"), model_outage.LLMRequestFailing), box
    assert srv.crashes == 1 + request_health.crash_resends()


def test_a_long_generation_cut_by_an_unrelated_restart_waits(monkeypatch):
    """The server went down long after the send: not evidence against this
    request — waited for, re-sent, and it goes through."""
    monkeypatch.setenv("AIFORGE_LLM_CRASH_WINDOW_S", "0.5")
    seen = {"n": 0}

    def answer(body, probe):
        if probe:
            return _OK
        seen["n"] += 1
        return ("crash", 1.0) if seen["n"] <= 5 else _OK
    srv = _Raw(answer, down_s=0.3)
    try:
        box = _bounded(lambda: model_wait.call_with_wait(
            lambda: _post(srv.url), url=srv.url, model="m"), 60)
    finally:
        srv.stop()
    assert box.get("out") == "hi", box


def test_a_flapping_proxy_502_on_request_and_probe_waits():
    """A tunnel / proxy flapping: the request 502s and so does the probe
    right after it; later probes pass. A proxy is not the model crashing —
    wait, resend, and it goes through."""
    st = {"posts": 0, "judge_next": False}

    def answer(body, probe):
        if probe:
            if st["judge_next"]:
                st["judge_next"] = False
                return 502, {"error": "bad gateway"}
            return _OK
        st["posts"] += 1
        if st["posts"] <= 8:
            st["judge_next"] = True
            return 502, {"error": "bad gateway"}
        return _OK
    srv = _Raw(answer)
    try:
        box = _bounded(lambda: model_wait.call_with_wait(
            lambda: _post(srv.url), url=srv.url, model="m"), 60)
    finally:
        srv.stop()
    assert box.get("out") == "hi", box


def test_our_own_network_dropping_waits(monkeypatch):
    """The stream is reset and the probe cannot even resolve the host (our
    network is down): an outage, never a crash — however often."""
    from aiforge_core.llm import _probe_states as ps
    monkeypatch.setattr(model_wait, "probe", lambda *a, **k: True)
    monkeypatch.setattr(model_wait, "live_probe",
                        lambda *a, **k: ps.State(ps.DOWN))
    calls = []

    def call():
        calls.append(1)
        if len(calls) <= 8:
            raise ConnectionResetError("connection reset by peer")
        return "ok"
    box = _bounded(lambda: model_wait.call_with_wait(
        call, url="http://m/v1", model="m"))
    assert box.get("out") == "ok", box


def test_an_answer_on_the_endpoint_resets_the_crash_count(monkeypatch):
    from aiforge_core.llm import _probe_states as ps
    monkeypatch.setattr(model_wait, "probe", lambda *a, **k: True)
    monkeypatch.setattr(model_wait, "live_probe",
                        lambda *a, **k: ps.State(ps.REFUSED))
    calls = []

    def call():
        calls.append(1)
        if len(calls) in (3, 5):           # someone else got an answer
            request_health.note_answer("http://m/v1")
        if len(calls) <= 6:
            raise ConnectionResetError("connection reset by peer")
        return "ok"
    box = _bounded(lambda: model_wait.call_with_wait(
        call, url="http://m/v1", model="m"))
    assert box.get("out") == "ok", box


def test_a_server_that_was_already_down_is_still_waited_for():
    """Down before the send and for many probes (not this request's doing):
    no crash is counted, the request goes through once it is back."""
    state = {"t0": time.monotonic()}

    def answer(body, probe):
        if time.monotonic() - state["t0"] < 1.0:
            return 502, {"error": "bad gateway"}
        return _OK
    srv = _Raw(answer)
    try:
        box = _bounded(lambda: model_wait.call_with_wait(
            lambda: _post(srv.url), url=srv.url, model="m"))
    finally:
        srv.stop()
    assert box.get("out") == "hi", box


def test_a_waiter_marks_the_resend_after_recovery(monkeypatch):
    monkeypatch.setattr(model_wait, "probe", lambda *a, **k: True)
    w = model_wait.Waiter("http://m/v1")
    w.wait(ConnectionRefusedError("refused"))
    assert w.health.up_at is not None
