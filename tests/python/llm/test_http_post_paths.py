"""The HTTP layer under every model call: sending, cancelling, metering.

Two POST paths exist for one reason. urllib blocks in ``getresponse()``, so a
generation the user hits Stop on would run to completion anyway; when a cancel
token is bound the request goes through ``http.client`` with a watcher thread
that closes the socket the instant Stop fires.

The ordering inside ``_post`` is load-bearing, and each step earned its place:

  * the preflight runs BEFORE the meter — against a sleeping box the toolbar
    used to read "18 requests · 18/min" with zero bytes on the wire, the meter
    inventing the overload it exists to diagnose;
  * the operator's ceiling waits AFTER the cancel check and preflight, so a
    parked call can still be stopped and an unreachable endpoint still fails
    fast;
  * a failed attempt is counted SEPARATELY from, not instead of, the request —
    a rate that dropped failures would read its lowest exactly when the
    endpoint is down.

``sent`` marks the moment the prompt becomes the server's problem: after that,
a retry duplicates work it is already doing.
"""
from __future__ import annotations

import io
import json
import threading
import types as pytypes
import urllib.error

import pytest

from aiforge_core.llm.client import _http as H
from aiforge_core.llm.types import Endpoint


def _ep(base_url="http://127.0.0.1:1234/v1", **kw):
    return Endpoint(base_url=base_url, api_key=kw.pop("api_key", "sk-test"),
                    model=kw.pop("model", "m"), provider=kw.pop("provider",
                                                                "local"),
                    role=kw.pop("role", "chat"), extras=kw.pop("extras", {}))


# ─── the request itself ────────────────────────────────────────────────


def test_the_client_identifies_itself(monkeypatch):
    """'curl/8.5.0 (aiforge)' identified neither the build nor the person, and
    gateway logs could not tell one user's traffic from another's."""
    headers = H._post_headers(_ep())
    assert headers["Authorization"] == "Bearer sk-test"
    assert headers["User-Agent"]
    assert not headers["User-Agent"].startswith("curl")


def test_the_user_agent_can_be_overridden_for_a_fussy_proxy(monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_USER_AGENT", "custom/1.0")
    assert H._post_headers(_ep())["User-Agent"] == "custom/1.0"


def test_a_url_is_split_into_host_port_and_path():
    conn, path = H._open_connection(_ep(), "http://box:8080/v1/chat?x=1", 5)
    assert (conn.host, conn.port) == ("box", 8080)
    assert path == "/v1/chat?x=1"
    assert conn.timeout == 5


def test_https_defaults_to_443_and_carries_a_tls_context():
    conn, path = H._open_connection(_ep(base_url="https://api.x.dev"),
                                    "https://api.x.dev/v1/chat", 5)
    assert conn.port == 443
    assert path == "/v1/chat"
    assert getattr(conn, "_context", None) is not None


def test_a_self_hosted_box_with_a_self_signed_cert_is_not_refused(monkeypatch):
    """The explicit opt-out (or a trusted internal host) selects the PINNED
    context — verification stays on, anchored to that box's own certificate
    (net.trust) — while a public host verifies against the ordinary roots."""
    from tests.python.tls_pin_fixture import stub_pin, trusts_the_pin

    stub_pin(monkeypatch)
    monkeypatch.setattr(H, "_ssl_ca_bundle", lambda: None)
    monkeypatch.setattr(H, "_ssl_auto_relax", lambda base: False)
    ctx = H._post_ctx(_ep(base_url="https://lan-box:8443/v1",
                          extras={"insecure_tls": True}))
    assert ctx.verify_mode.name == "CERT_REQUIRED"
    assert trusts_the_pin(ctx), "the opt-out must trust that box's own cert"


def test_a_ca_bundle_keeps_verification_on(monkeypatch):
    monkeypatch.setattr(H, "_ssl_ca_bundle", lambda: "/etc/ca.pem")
    monkeypatch.setattr(H, "_ssl_context_for", lambda base: "verified")
    assert H._post_ctx(_ep(base_url="https://lan:8443/v1",
                           extras={"insecure_tls": True})) == "verified"


def test_plain_http_uses_the_ordinary_context(monkeypatch):
    monkeypatch.setattr(H, "_ssl_context_for", lambda base: "default")
    assert H._post_ctx(_ep()) == "default"


# ─── reading the response ──────────────────────────────────────────────


class _Resp:
    def __init__(self, status=200, body=b'{"choices": []}', reason="OK"):
        self.status, self.reason = status, reason
        self._body = body
        self.headers = {}

    def read(self):
        return self._body


class _Conn:
    def __init__(self, resp=None, raise_on=None):
        self.resp = resp or _Resp()
        self.closed = False
        self.requested = None
        self._raise = raise_on

    def request(self, method, path, body=None, headers=None):
        self.requested = {"method": method, "path": path, "body": body,
                          "headers": headers}
        if self._raise:
            raise self._raise

    def getresponse(self):
        return self.resp

    def close(self):
        self.closed = True


def test_a_good_response_is_parsed():
    assert H._read_http_response(_Conn(), "http://x") == {"choices": []}


def test_an_error_status_is_raised_like_urllib_would():
    """So the retry classifier handles both paths identically."""
    conn = _Conn(_Resp(status=429, body=b"slow down", reason="Too Many"))
    with pytest.raises(urllib.error.HTTPError) as exc:
        H._read_http_response(conn, "http://x")
    assert exc.value.code == 429


def test_a_200_that_says_the_model_dropped_is_transient(monkeypatch):
    body = json.dumps({"error": {"message": "model not loaded"}}).encode()
    seen: list = []
    monkeypatch.setattr(H, "_raise_if_model_dropped",
                        lambda b: seen.append(b))
    H._read_http_response(_Conn(_Resp(body=body)), "http://x")
    assert seen
    assert "error" in seen[0]


# ─── the cancellable path ──────────────────────────────────────────────


@pytest.fixture
def cancellable(monkeypatch):
    state: dict = {"conn": _Conn()}
    monkeypatch.setattr(H, "_open_connection",
                        lambda ep, url, t: (state["conn"], "/v1/chat"))
    return state


def test_a_normal_generation_posts_and_returns(cancellable):
    cancel = threading.Event()
    sent = [False]
    out = H._post_cancellable(_ep(), b"{}", 30, cancel, sent)
    assert out == {"choices": []}
    assert sent == [True]
    assert cancellable["conn"].requested["method"] == "POST"
    assert cancellable["conn"].closed is True


def test_stopping_before_the_request_sends_nothing(cancellable):
    cancel = threading.Event()
    cancel.set()
    ep = _ep()
    with pytest.raises(H._LLMCancelled):
        H._post_cancellable(ep, b"{}", 30, cancel, [False])
    assert cancellable["conn"].requested is None


def test_a_socket_error_after_stop_reads_as_cancelled(cancellable):
    """The watcher closed the connection — that is a stop, not a failure."""
    cancel = threading.Event()

    def _request(*a, **kw):
        cancel.set()
        raise OSError("connection closed")
    cancellable["conn"].request = _request
    ep = _ep()
    with pytest.raises(H._LLMCancelled):
        H._post_cancellable(ep, b"{}", 30, cancel, [False])


def test_a_genuine_socket_error_is_not_disguised_as_a_stop(cancellable):
    cancellable["conn"]._raise = OSError("connection refused")
    ep, never = _ep(), threading.Event()
    with pytest.raises(OSError, match="refused"):
        H._post_cancellable(ep, b"{}", 30, never, [False])


def test_the_watcher_closes_the_socket_the_instant_stop_fires():
    """Which is what unblocks getresponse() on the main thread."""
    conn = _Conn()
    cancel = threading.Event()
    stop = threading.Event()
    H._cancel_watcher(conn, cancel, stop)
    cancel.set()
    for _ in range(100):
        if conn.closed:
            break
        threading.Event().wait(0.02)
    stop.set()
    assert conn.closed is True


def test_the_watcher_stops_when_the_request_finishes():
    conn = _Conn()
    stop = threading.Event()
    H._cancel_watcher(conn, threading.Event(), stop)
    stop.set()
    threading.Event().wait(0.05)
    assert conn.closed is False


# ─── the preflight ─────────────────────────────────────────────────────


@pytest.fixture
def socket_probe(monkeypatch):
    import socket
    state: dict = {"raise": None, "seen": None}

    def _connect(addr, timeout=None):
        state["seen"] = (addr, timeout)
        if state["raise"]:
            raise state["raise"]
        return pytypes.SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(socket, "create_connection", _connect)
    return state


def test_a_reachable_endpoint_passes_quickly(socket_probe, monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_CONNECT_TIMEOUT_S", "2")
    H._preflight("http://box:1234/v1")
    assert socket_probe["seen"] == (("box", 1234), 2.0)


def test_an_asleep_host_fails_in_seconds_not_ten_minutes(socket_probe):
    """One scalar timeout covers connect AND read, so without this a dropped
    SYN blocks the full 600s request budget just to fail the TCP connect."""
    socket_probe["raise"] = OSError("no route to host")
    with pytest.raises(ConnectionError, match="unreachable"):
        H._preflight("http://box:1234/v1")


def test_the_preflight_can_be_switched_off(socket_probe, monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_CONNECT_TIMEOUT_S", "0")
    H._preflight("http://box:1234/v1")
    assert socket_probe["seen"] is None


def test_https_probes_443_by_default(socket_probe):
    H._preflight("https://api.x.dev/v1")
    assert socket_probe["seen"][0] == ("api.x.dev", 443)


def test_a_malformed_url_is_left_to_the_real_call(socket_probe):
    H._preflight("not a url")
    assert socket_probe["seen"] is None


# ─── the meter ─────────────────────────────────────────────────────────


@pytest.fixture
def meter(monkeypatch):
    from aiforge_core.llm import call_meter
    state: dict = {"records": [], "failures": [], "token": "tok"}
    monkeypatch.setattr(call_meter, "record",
                        lambda role=None, provider=None, model=None:
                        state["records"].append((role, provider, model))
                        or state["token"])
    monkeypatch.setattr(call_meter, "record_failure",
                        lambda tok, reason: state["failures"].append((tok,
                                                                      reason)))
    return state


def test_a_send_is_counted_with_who_sent_it(meter):
    """The background daemon has no request context, and its calls are exactly
    the ones the toolbar meter exists to make visible."""
    assert H._record_request("learner", "local", "qwen") == "tok"
    assert meter["records"] == [("learner", "local", "qwen")]


def test_a_broken_meter_never_breaks_a_call(monkeypatch):
    from aiforge_core.llm import call_meter
    monkeypatch.setattr(call_meter, "record",
                        lambda **kw: (_ for _ in ()).throw(OSError("db")))
    assert H._record_request("chat") is None


def test_a_failure_is_charged_to_the_request_that_failed(meter):
    H._record_failure("tok", TimeoutError("read timed out"))
    assert meter["failures"]
    assert meter["failures"][0][0] == "tok"


def test_an_uncounted_send_has_no_failure_to_count(meter):
    """Counting it anyway would put `failed` above `total`."""
    H._record_failure(None, OSError("x"))
    assert meter["failures"] == []


def test_a_classifier_that_blows_up_does_not_escape(meter, monkeypatch):
    monkeypatch.setattr(H, "_is_transient_exc",
                        lambda exc: (_ for _ in ()).throw(RuntimeError("x")))
    H._record_failure("tok", OSError("boom"))
    assert meter["failures"][0][1] == "OSError"


# ─── the whole send ────────────────────────────────────────────────────


@pytest.fixture
def post(monkeypatch, meter):
    """_post with its rate limiter, preflight and transport stubbed."""
    state: dict = {"acquired": [], "governed": [], "preflights": [],
                   "response": {"choices": [{"message": {"content": "hi"}}]},
                   "raise": None, "throttled": 0.0}
    monkeypatch.setattr(H._rl, "acquire",
                        lambda provider, **kw: state["acquired"].append(kw))
    monkeypatch.setattr(H._rl, "govern_send",
                        lambda **kw: state["governed"].append(kw)
                        or (state["throttled"], None))
    monkeypatch.setattr(H, "_preflight",
                        lambda base: state["preflights"].append(base))

    def _cancellable(ep, payload, timeout, cancel, sent=None):
        if sent is not None:
            sent[0] = True
        if state["raise"]:
            raise state["raise"]
        return state["response"]
    monkeypatch.setattr(H, "_post_cancellable", _cancellable)
    H.set_cancel_event(None)
    yield state
    H.set_cancel_event(None)


def test_a_call_waits_for_budget_then_sends(post, meter, monkeypatch):
    import urllib.request

    class _R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(post["response"]).encode()
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None, context=None: _R())
    sent = [False]
    out = H._post(_ep(), b"{}", 30, role="chat", sent=sent)
    assert out == post["response"]
    assert sent == [True]
    assert post["preflights"] == ["http://127.0.0.1:1234/v1"]
    assert meter["records"] == [("chat", "local", "m")]


def test_the_preflight_runs_before_anything_is_counted(post, meter,
                                                       monkeypatch):
    """Against a sleeping box the toolbar read '18 requests' with zero bytes
    on the wire."""
    monkeypatch.setattr(H, "_preflight",
                        lambda base: (_ for _ in ()).throw(
                            ConnectionError("unreachable")))
    ep = _ep()
    with pytest.raises(ConnectionError):
        H._post(ep, b"{}", 30, role="chat")
    assert meter["records"] == [], "nothing was sent, nothing is counted"


def test_a_bound_cancel_token_takes_the_cancellable_path(post, meter):
    cancel = threading.Event()
    H.set_cancel_event(cancel)
    assert H._post(_ep(), b"{}", 30, role="chat") == post["response"]
    assert meter["records"] == [("chat", "local", "m")]


def test_a_run_stopped_before_the_send_counts_nothing(post, meter):
    cancel = threading.Event()
    cancel.set()
    H.set_cancel_event(cancel)
    ep = _ep()
    with pytest.raises(H._LLMCancelled):
        H._post(ep, b"{}", 30, role="chat")
    assert meter["records"] == []
    assert post["preflights"] == []


def test_a_failed_send_is_counted_as_traffic_and_as_a_failure(post, meter):
    """The provider billed and rate-limited it either way."""
    cancel = threading.Event()
    H.set_cancel_event(cancel)
    post["raise"] = OSError("connection reset")
    ep = _ep()
    with pytest.raises(OSError):
        H._post(ep, b"{}", 30, role="chat")
    assert len(meter["records"]) == 1
    assert len(meter["failures"]) == 1


def test_the_callers_budget_bounds_the_rate_limit_wait(post):
    """A 15s classifier could otherwise block for over two minutes inside a
    chain the log called a 25s budget."""
    cancel = threading.Event()
    H.set_cancel_event(cancel)
    H._post(_ep(), b"{}", 30, role="chat", max_wait_s=10)
    assert post["acquired"][0]["max_wait_s"] == 10.0


def test_time_spent_queued_is_reported_back(post):
    cancel = threading.Event()
    H.set_cancel_event(cancel)
    post["throttled"] = 4.0
    throttled = [0.0]
    H._post(_ep(), b"{}", 30, role="chat", throttled=throttled)
    assert throttled == [4.0]


def test_the_meter_token_is_handed_up_to_the_caller(post, meter):
    """A 200-OK whose content is empty raises nothing, so only the caller
    reading the body can charge that failure to the right minute."""
    cancel = threading.Event()
    H.set_cancel_event(cancel)
    token = [None]
    H._post(_ep(), b"{}", 30, role="chat", meter=token)
    assert token == ["tok"]


# ─── retry arithmetic ──────────────────────────────────────────────────


def _err(code=429, headers=None):
    return urllib.error.HTTPError("http://x", code, "err", headers or {},
                                  io.BytesIO(b""))


def test_a_retry_after_header_is_read():
    assert H._retry_after_s(_err(headers={"Retry-After": "12"})) == 12.0


@pytest.mark.parametrize("headers", [{}, {"Retry-After": "in a bit"}])
def test_an_absent_or_unparseable_header_is_no_hint(headers):
    assert H._retry_after_s(_err(headers=headers)) is None


def test_a_non_http_error_has_no_header():
    assert H._retry_after_s(OSError("reset")) is None


def test_a_429_backoff_is_told_to_the_limiter(monkeypatch):
    """The rejection is the only ground truth about what the server counts —
    our ceiling is per-process and cannot see the memory daemon."""
    noted: list = []
    monkeypatch.setattr(H._rl, "note_rate_limited",
                        lambda ra, provider=None: noted.append((ra, provider)))
    H._rate_limited_sleep(0.5, 30.0, "gemini")
    assert noted == [(30.0, "gemini")]


def test_a_hostile_retry_after_cannot_park_the_process(monkeypatch):
    """'Retry-After: 3600' would otherwise be two hours of blocking sleep."""
    monkeypatch.setattr(H._rl, "note_rate_limited", lambda *a, **k: None)
    monkeypatch.setattr(H._rl, "_setting",
                        lambda name, env, default: 60.0 if "cap" in name
                        else 20.0)
    assert H._rate_limited_sleep(0.5, 3600.0, "gemini") == 60.0


def test_a_429_without_a_header_uses_the_configured_backoff(monkeypatch):
    monkeypatch.setattr(H._rl, "note_rate_limited", lambda *a, **k: None)
    monkeypatch.setattr(H._rl, "_setting",
                        lambda name, env, default: 60.0 if "cap" in name
                        else 20.0)
    assert H._rate_limited_sleep(0.5, None, "gemini") == 20.0


def test_a_limiter_hint_that_fails_does_not_break_the_backoff(monkeypatch):
    monkeypatch.setattr(H._rl, "note_rate_limited",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    assert H._rate_limited_sleep(1.0, 5.0, "local") > 0


def test_the_budget_leaves_room_for_a_whole_attempt(monkeypatch):
    """A retry gets the full per-attempt timeout or it is not made."""
    cfg = H._RetryCfg(timeout_s=30)
    cfg.deadline = H.time.monotonic() + 10
    assert H._budget_exhausted(cfg, attempt=1, retry=True, sleep_s=1) is True
    cfg.deadline = H.time.monotonic() + 300
    assert H._budget_exhausted(cfg, attempt=1, retry=True, sleep_s=1) is False


def test_a_disabled_budget_never_refuses_a_retry(monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_RETRY_BUDGET", "0")
    cfg = H._RetryCfg(timeout_s=30)
    assert cfg.deadline is None
    assert cfg.left() is None
    assert H._budget_exhausted(cfg, attempt=1, retry=True, sleep_s=99) is False


def test_time_spent_queued_is_given_back_to_the_budget():
    """Queue time is not time this attempt spent failing."""
    cfg = H._RetryCfg(timeout_s=30)
    before = cfg.deadline
    cfg.extend(12.0)
    assert cfg.deadline == before + 12.0
    cfg.extend(-5)
    assert cfg.deadline == before + 12.0


def test_a_read_timeout_is_not_re_posted():
    """The server has the prompt and is working on it."""
    cfg = H._RetryCfg(timeout_s=30)
    assert H._timeout_already_shipped(cfg, attempt=1, label="timeout",
                                      retry=True, sent=True) is True
    assert H._timeout_already_shipped(cfg, attempt=1, label="timeout",
                                      retry=True, sent=False) is False
    assert H._timeout_already_shipped(cfg, attempt=1, label="transport",
                                      retry=True, sent=True) is False


def test_the_backoff_grows_and_is_capped(monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_RETRY_BASE_S", "1")
    monkeypatch.setenv("AIFORGE_LLM_RETRY_CAP_S", "4")
    monkeypatch.setattr(H.random, "uniform", lambda a, b: 0.0)
    cfg = H._RetryCfg(timeout_s=10)
    assert H._next_sleep(cfg, 1, OSError("x"), "transport", "local") == 1
    assert H._next_sleep(cfg, 2, OSError("x"), "transport", "local") == 2
    assert H._next_sleep(cfg, 9, OSError("x"), "transport", "local") == 4


def test_a_retry_after_overrides_the_exponential_backoff(monkeypatch):
    monkeypatch.setattr(H.random, "uniform", lambda a, b: 0.0)
    monkeypatch.setenv("AIFORGE_LLM_RETRY_CAP_S", "30")
    cfg = H._RetryCfg(timeout_s=10)
    exc = _err(code=500, headers={"Retry-After": "7"})
    assert H._next_sleep(cfg, 1, exc, "transport", "local") == 7


def test_a_throttled_callers_wait_is_clamped_to_the_room_it_has(monkeypatch):
    """A flat 20s + timeout never fits a 15-30s classifier, so it got the long
    backoff computed for it and then no retry at all."""
    monkeypatch.setattr(H.random, "uniform", lambda a, b: 0.0)
    monkeypatch.setattr(H._rl, "note_rate_limited", lambda *a, **k: None)
    monkeypatch.setattr(H._rl, "_setting",
                        lambda name, env, default: 60.0 if "cap" in name
                        else 20.0)
    cfg = H._RetryCfg(timeout_s=10)
    cfg.deadline = H.time.monotonic() + 12
    sleep_s = H._next_sleep(cfg, 1, _err(), "rate_limited", "gemini")
    assert 0 <= sleep_s < 20, "shortened, not refused"
