"""Chat failover (llm/model_failover) and the mid-run model switch
(llm/model_switch), driven through the real retry loop (_post_with_retry)
with a fake ``_post`` — and, where the bound is a socket timeout, a real
local server.

The rules under test:
* a stall on the chat's model sends the SAME request to the next listed model,
  says so in the chat, and the rest of the turn stays there;
* a slow answer that keeps streaming never fails over;
* AIFORGE_LLM_FAILOVER=0 restores the old behaviour exactly;
* a model picked mid-run aborts the request waiting on the old model and
  retries that step on the new one; with nothing in flight the next step just
  uses it.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import replace

import pytest

from aiforge_core.config import chat_failover
from aiforge_core.llm import _wait_scope, model_failover, model_switch, request_health
from aiforge_core.llm.client import _http, _http_retry
from aiforge_core.llm.client._stream_health import LLMStreamStalled
from aiforge_core.llm.types import Endpoint
from aiforge_core.runtime import request_context

SID = "4242"
OK = {"choices": [{"message": {"role": "assistant", "content": "done"}}]}


def _ep(model: str, url: str = "http://box:1234/v1") -> Endpoint:
    return Endpoint(base_url=url, api_key="k", model=model,
                    provider="openai_compatible", role="chat", extras={})


def _payload(model: str) -> bytes:
    return json.dumps({"model": model, "messages": [{"role": "user", "content": "hi"}]}).encode()


def _no_backoff(monkeypatch):
    """No retry backoff in the retry module ONLY — patching ``time.sleep``
    itself would also stop the test servers below from being silent."""
    import types
    monkeypatch.setattr(_http_retry, "time", types.SimpleNamespace(
        sleep=lambda s: None, monotonic=time.monotonic))


@pytest.fixture
def chat(monkeypatch):
    """A chat turn on model-a, with model-b then model-c listed for failover.
    Yields the state: the chat's pick, the posts made, the lines shown."""
    state = {"pick": "model-a", "url": "http://box:1234/v1", "posts": [], "said": []}
    monkeypatch.delenv("AIFORGE_LLM_FAILOVER", raising=False)
    rows = [{"id": "b", "model": "model-b"}, {"id": "c", "model": "model-c"}]
    monkeypatch.setattr(chat_failover, "model_ids", lambda: ["b", "c"])
    monkeypatch.setattr(chat_failover, "rows", lambda: [dict(r) for r in rows])
    monkeypatch.setattr(chat_failover, "stall_s", lambda: 180)
    monkeypatch.setattr(chat_failover, "fails", lambda: 2)

    from aiforge_core.llm import router
    monkeypatch.setattr(router, "resolve",
                        lambda role: replace(_ep(state["pick"], state["url"]), role=role))
    # The server does not answer /models: a first-token stall fails over at once.
    monkeypatch.setattr(model_failover, "_server_alive", lambda ep: False)
    monkeypatch.setattr(_http_retry, "_endpoint_down", lambda ep: False)
    _no_backoff(monkeypatch)
    monkeypatch.setattr(_http_retry, "_say_retry", lambda *a, **k: None)
    model_switch.forget()
    tok = request_context.set_session_id(SID)
    turn = request_health.bind_turn()
    with _wait_scope.status_sink(lambda st: state["said"].append(st.get("text", ""))):
        yield state
    request_health.end_turn(turn)
    request_context.reset_session_id(tok)
    model_switch.forget()


def _fake_post(monkeypatch, state, behave):
    """``behave(model, payload)`` returns a body or raises."""
    def _post(ep, payload, timeout_s, *, role=None, sent=None, max_wait_s=None,
              throttled=None, **_):
        assert json.loads(payload)["model"] == ep.model   # body re-addressed too
        state["posts"].append(ep.model)
        state.setdefault("hosts", []).append(ep.base_url)
        return behave(ep.model, payload)
    monkeypatch.setattr(_http, "_post", _post)


def _send(model="model-a", role="chat", url="http://box:1234/v1"):
    return _http_retry._post_with_retry(replace(_ep(model, url), role=role),
                                        _payload(model), 60, role=role, source="native")


# ── failover on a stall ─────────────────────────────────────────────────

def test_a_stall_fails_over_to_the_next_listed_model_and_sticks(chat, monkeypatch):
    def behave(model, _):
        if model == "model-a":
            raise LLMStreamStalled("first_token", 180)
        return OK
    _fake_post(monkeypatch, chat, behave)
    assert _send() == OK
    assert chat["posts"] == ["model-a", "model-b"]
    assert any("model model-a not responding" in s and "switched to model-b" in s
               for s in chat["said"])
    # The next step of the turn resolves model-a again: it goes to model-b.
    chat["posts"].clear()
    assert _send() == OK
    assert chat["posts"] == ["model-b"]


def test_the_next_model_stalling_too_moves_on_and_never_back(chat, monkeypatch):
    def behave(model, _):
        if model in ("model-a", "model-b"):
            raise LLMStreamStalled("idle", 180)
        return OK
    _fake_post(monkeypatch, chat, behave)
    assert _send() == OK
    assert chat["posts"] == ["model-a", "model-b", "model-c"]


def test_every_listed_model_failing_ends_in_the_ordinary_error(chat, monkeypatch):
    def behave(model, _):
        raise LLMStreamStalled("first_token", 180)
    _fake_post(monkeypatch, chat, behave)
    with pytest.raises(LLMStreamStalled):
        _send()
    # a, b, c once each by failover; then the last model keeps the old retries
    assert chat["posts"][:3] == ["model-a", "model-b", "model-c"]
    assert set(chat["posts"][3:]) == {"model-c"}


def test_repeated_transport_failures_fail_over(chat, monkeypatch):
    def behave(model, _):
        if model == "model-a":
            raise ConnectionRefusedError("refused")
        return OK
    _fake_post(monkeypatch, chat, behave)
    assert _send() == OK
    assert chat["posts"] == ["model-a", "model-a", "model-b"]   # fails() == 2


def test_a_rate_limit_never_fails_over(chat, monkeypatch):
    import io
    import urllib.error

    def behave(model, _):
        raise urllib.error.HTTPError("u", 429, "slow down", {}, io.BytesIO(b""))
    _fake_post(monkeypatch, chat, behave)
    monkeypatch.setattr(_http_retry, "_rate_limited_sleep", lambda s, ra, p: 0.0)
    with pytest.raises(urllib.error.HTTPError):
        _send()
    assert set(chat["posts"]) == {"model-a"}


def test_outside_a_chat_nothing_changes(monkeypatch):
    _no_backoff(monkeypatch)
    monkeypatch.setattr(_http_retry, "_endpoint_down", lambda ep: False)
    posts = []

    def _post(ep, payload, timeout_s, **_):
        posts.append(ep.model)
        raise LLMStreamStalled("first_token", 1)
    monkeypatch.setattr(_http, "_post", _post)
    assert model_failover.begin(_ep("model-a"), _payload("model-a"), "chat") is None
    with pytest.raises(LLMStreamStalled):
        _send()
    assert set(posts) == {"model-a"}


# ── the off switch ──────────────────────────────────────────────────────

def test_the_off_switch_restores_the_old_behaviour(chat, monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_FAILOVER", "0")

    def behave(model, _):
        raise LLMStreamStalled("first_token", 180)
    _fake_post(monkeypatch, chat, behave)
    with pytest.raises(LLMStreamStalled):
        _send()
    assert set(chat["posts"]) == {"model-a"}
    g = model_failover.begin(_ep("model-a"), _payload("model-a"), "chat")
    with g.bound():
        assert model_failover.stall_cap() == 0.0
    g.close()


def test_no_list_means_no_failover(chat, monkeypatch):
    monkeypatch.setattr(chat_failover, "model_ids", lambda: [])
    monkeypatch.setattr(chat_failover, "rows", lambda: [])

    def behave(model, _):
        raise LLMStreamStalled("first_token", 180)
    _fake_post(monkeypatch, chat, behave)
    with pytest.raises(LLMStreamStalled):
        _send()
    assert set(chat["posts"]) == {"model-a"}


# ── the stream bound, on a real socket ──────────────────────────────────

def _serve(script):
    """One-shot SSE server playing ("sleep", s) / ("send", bytes) steps."""
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
            for kind, arg in script:
                if kind == "sleep":
                    time.sleep(arg)
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


_HEAD = b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n"


def _tok(text):
    return ("data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": text}}]})
            + "\n\n").encode()


def _read(port):
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    conn.request("POST", "/v1/chat/completions", body=b"{}")
    try:
        return _http._read_sse_response(conn, "u", lambda k, t: None, payload=b"{}",
                                        read_timeout=60)
    finally:
        conn.close()


@pytest.fixture
def long_bounds(monkeypatch):
    """The ordinary bounds are long: only the failover cap can cut a send."""
    monkeypatch.setenv("AIFORGE_LLM_FIRST_TOKEN_S", "30")
    monkeypatch.setenv("AIFORGE_LLM_STREAM_IDLE_S", "30")
    monkeypatch.setenv("AIFORGE_LLM_STALL_BUDGET_S", "0")


def test_silence_past_the_failover_threshold_is_a_stall(chat, monkeypatch, long_bounds):
    monkeypatch.setattr(chat_failover, "stall_s", lambda: 0.6)
    port = _serve([("send", _HEAD), ("sleep", 4)])
    g = model_failover.begin(_ep("model-a"), _payload("model-a"), "chat")
    t0 = time.monotonic()
    with g.bound(), pytest.raises(LLMStreamStalled):
        _read(port)
    g.close()
    assert time.monotonic() - t0 < 3


def test_a_slow_answer_that_keeps_streaming_never_fails_over(chat, monkeypatch,
                                                              long_bounds):
    monkeypatch.setattr(chat_failover, "stall_s", lambda: 0.6)
    steps = [("send", _HEAD)]
    for i in range(6):                       # 2.4 s in all, 0.4 s apart
        steps += [("sleep", 0.4), ("send", _tok(f"t{i} "))]
    steps += [("send", b"data: [DONE]\n\n")]
    port = _serve(steps)
    g = model_failover.begin(_ep("model-a"), _payload("model-a"), "chat")
    with g.bound():
        body = _read(port)
    g.close()
    assert body["choices"][0]["message"]["content"].startswith("t0 t1")


# ── the user changing the model mid-run ─────────────────────────────────

def test_a_mid_run_pick_aborts_the_request_and_retries_on_the_new_model(chat,
                                                                         monkeypatch):
    started = threading.Event()

    def behave(model, _):
        if model == "model-a":
            started.set()
            abort = model_failover.abort_check()
            end = time.monotonic() + 5
            while time.monotonic() < end and not abort():
                time.sleep(0.02)
            raise model_failover.ModelSwitched()
        return OK
    _fake_post(monkeypatch, chat, behave)

    out = {}
    ctx = __import__("contextvars").copy_context()
    t = threading.Thread(target=lambda: out.update(r=ctx.run(_send)))
    t.start()
    assert started.wait(2)
    chat["pick"] = "model-z"                       # saved as the chat's model
    assert model_switch.request(SID, "model-a", "model-z") == "retrying"
    t.join(5)
    assert out["r"] == OK
    assert chat["posts"] == ["model-a", "model-z"]
    assert any("switched to model-z — retrying the step" in s for s in chat["said"])


def test_a_pick_with_nothing_in_flight_just_applies_next_step(chat, monkeypatch):
    _fake_post(monkeypatch, chat, lambda model, _: OK)
    chat["pick"] = "model-z"
    assert model_switch.request(SID, "model-a", "model-z") == "next_step"
    assert _send("model-z") == OK
    assert chat["posts"] == ["model-z"]
    # A step that resolved the OLD model just before the pick changed is sent
    # on the new one — silently, the step was never tried.
    chat["posts"].clear()
    model_switch.request(SID, "model-z", "model-y")
    chat["pick"] = "model-y"
    assert _send("model-z") == OK
    assert chat["posts"] == ["model-y"]
    assert not any("retrying the step" in s for s in chat["said"])


def test_a_user_pick_ends_the_turns_failover(chat, monkeypatch):
    def behave(model, _):
        if model == "model-a":
            raise LLMStreamStalled("first_token", 180)
        return OK
    _fake_post(monkeypatch, chat, behave)
    _send()                                          # turn now sticks to model-b
    chat["pick"] = "model-a"
    model_switch.request(SID, "model-b", "model-a")  # the user goes back to a
    chat["posts"].clear()
    _fake_post(monkeypatch, chat, lambda model, _: OK)
    assert _send("model-a") == OK
    assert chat["posts"] == ["model-a"]


def test_the_cancellable_post_is_cut_by_a_switch(chat, monkeypatch):
    """Real socket: a server that never answers, the user picks another model
    — the connection is closed at once and ModelSwitched raised (not a
    cancel, not a stall)."""
    import threading as th
    monkeypatch.setenv("AIFORGE_LLM_FIRST_TOKEN_S", "30")
    port = _serve([("sleep", 6)])
    ep = replace(_ep("model-a"), base_url=f"http://127.0.0.1:{port}/v1")
    g = model_failover.begin(ep, _payload("model-a"), "chat")
    th.Timer(0.4, lambda: model_switch.request(SID, "model-a", "model-z")).start()
    t0 = time.monotonic()
    with g.bound(), pytest.raises(model_failover.ModelSwitched):
        _http._post_cancellable_once(ep, _payload("model-a"), 60, th.Event(), None, None)
    g.close()
    assert time.monotonic() - t0 < 3


# ── review fixes ────────────────────────────────────────────────────────

def test_a_live_server_gets_one_more_send_before_failing_over(chat, monkeypatch):
    """/models answers: a slow prefill, not a dead model — the same model is
    sent once more (its bounds doubled); a second silent send fails over."""
    monkeypatch.setattr(model_failover, "_server_alive", lambda ep: True)

    def behave(model, _):
        if model == "model-a":
            raise LLMStreamStalled("first_token", 180)
        return OK
    _fake_post(monkeypatch, chat, behave)
    assert _send() == OK
    assert chat["posts"] == ["model-a", "model-a", "model-b"]


def test_an_unresponsive_server_fails_over_at_once(chat, monkeypatch):
    probed = []
    monkeypatch.setattr(model_failover, "_server_alive",
                        lambda ep: probed.append(ep.model) or False)

    def behave(model, _):
        if model == "model-a":
            raise LLMStreamStalled("first_token", 180)
        return OK
    _fake_post(monkeypatch, chat, behave)
    assert _send() == OK
    assert chat["posts"] == ["model-a", "model-b"] and probed == ["model-a"]


def _watch_with_guard(payload, url="http://box:1234/v1"):
    from aiforge_core.llm.client._stream_health import StreamWatch
    g = model_failover.begin(_ep("model-a"), _payload("model-a"), "chat")
    with g.bound():
        w = StreamWatch(None, payload, 900, url=url)
    g.close()
    return w


def test_a_big_prompt_on_a_slow_server_keeps_its_prefill_time(chat, monkeypatch):
    """40k tokens at a learned 150 tok/s is ~270 s of prefill: the 180 s
    failover cap must not cut it, and a re-send still doubles."""
    monkeypatch.setenv("AIFORGE_LLM_FIRST_TOKEN_S", "180")
    monkeypatch.setattr(request_health, "prefill_tps", lambda url: 150.0)
    big = b"x" * (40_000 * 4)
    w = _watch_with_guard(big)
    assert w._cap == 180 and w._first >= 260
    monkeypatch.setattr(request_health, "stall_scale", lambda: 2.0)
    assert _watch_with_guard(big)._first >= 520
    # A small prompt is cut at the cap (the ordinary bound is longer).
    monkeypatch.setenv("AIFORGE_LLM_FIRST_TOKEN_S", "600")
    monkeypatch.setattr(request_health, "stall_scale", lambda: 1.0)
    assert _watch_with_guard(b"{}")._first == 180


def test_keep_alives_during_prefill_are_not_cut_by_the_cap(chat, monkeypatch, long_bounds):
    monkeypatch.setattr(chat_failover, "stall_s", lambda: 0.6)
    steps = [("send", _HEAD)]
    for _ in range(8):                       # 2.4 s of keep-alives, no token
        steps += [("sleep", 0.3), ("send", b": keep-alive\n\n")]
    steps += [("send", _tok("hello")), ("send", b"data: [DONE]\n\n")]
    port = _serve(steps)
    g = model_failover.begin(_ep("model-a"), _payload("model-a"), "chat")
    with g.bound():
        body = _read(port)
    g.close()
    assert body["choices"][0]["message"]["content"] == "hello"


def test_the_same_id_on_another_server_is_a_switch(chat, monkeypatch):
    started = threading.Event()

    def behave(model, _):
        if chat["hosts"][-1] == "http://box:1234/v1":
            started.set()
            abort = model_failover.abort_check()
            end = time.monotonic() + 5
            while time.monotonic() < end and not abort():
                time.sleep(0.02)
            raise model_failover.ModelSwitched()
        return OK
    _fake_post(monkeypatch, chat, behave)
    out = {}
    ctx = __import__("contextvars").copy_context()
    t = threading.Thread(target=lambda: out.update(r=ctx.run(_send)))
    t.start()
    assert started.wait(2)
    chat["url"] = "http://box2:1234/v1"
    assert model_switch.request(SID, "model-a", "model-a", from_url="http://box:1234/v1",
                                to_url="http://box2:1234/v1") == "retrying"
    t.join(5)
    assert out["r"] == OK
    assert chat["hosts"] == ["http://box:1234/v1", "http://box2:1234/v1"]
    assert model_switch.request(SID, "m", "m", "http://a/v1", "http://a/") == ""


def test_a_role_with_its_own_model_never_fails_over(chat, monkeypatch):
    from aiforge_core.config.agent_config import _resolve
    monkeypatch.setattr(_resolve, "_roles_with_own_model", lambda: frozenset({"coder"}))

    def behave(model, _):
        raise LLMStreamStalled("first_token", 180)
    _fake_post(monkeypatch, chat, behave)
    with pytest.raises(LLMStreamStalled):
        _send("coder-x", role="coder")
    assert set(chat["posts"]) == {"coder-x"}


def test_a_pick_during_the_retry_backoff_is_applied_at_once(chat, monkeypatch):
    import types
    monkeypatch.setattr(chat_failover, "fails", lambda: 20)    # no failover
    slept = []

    def sleep(s):
        slept.append(s)
        if len(slept) == 1:                  # the user picks mid-backoff
            chat["pick"] = "model-z"
            model_switch.request(SID, "model-a", "model-z")
    monkeypatch.setattr(_http_retry, "time", types.SimpleNamespace(
        sleep=sleep, monotonic=time.monotonic))

    def behave(model, _):
        if model == "model-a":
            raise ConnectionRefusedError("refused")
        return OK
    _fake_post(monkeypatch, chat, behave)
    assert _send() == OK
    assert chat["posts"] == ["model-a", "model-z"]
    assert len(slept) == 1                    # the rest of the backoff was skipped
    assert any("switched to model-z" in s for s in chat["said"])


def test_a_wait_for_a_down_model_ends_on_a_pick(chat):
    from aiforge_core.llm import model_wait
    fired = model_wait._switch_watch()
    assert fired() is False
    model_switch.request(SID, "model-a", "model-z")
    assert fired() is True and fired() is False          # once per switch
    w = model_wait.Waiter("http://box:1234/v1")
    w.switched = lambda: True
    t0 = time.monotonic()
    w._sleep_cancellable(5, ConnectionRefusedError())
    assert time.monotonic() - t0 < 1 and w._switch_hit


def test_parallel_failovers_share_one_record(chat, monkeypatch):
    """Two step threads of one turn failing over at once: the record stays
    consistent (model-a tried once, both on model-b)."""
    def behave(model, _):
        if model == "model-a":
            raise LLMStreamStalled("first_token", 180)
        return OK
    _fake_post(monkeypatch, chat, behave)
    ctx = __import__("contextvars").copy_context
    ts = [threading.Thread(target=lambda c=ctx(): c.run(_send)) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(5)
    fo = request_health.turn_state()["failover"]
    assert fo["tried"] == ["model-a@http://box:1234"] and fo["to"]["model"] == "model-b"


# ── round-2 review fixes ────────────────────────────────────────────────

def test_the_outage_wait_follows_a_user_switch(chat, monkeypatch):
    """The waiter is rebuilt when the request's model changes: it must probe
    the model the request goes to now, not the one it first failed on."""
    from aiforge_core.llm import model_wait
    seen, calls = [], {"n": 0}

    def wait(self, exc):
        seen.append(self.model)
        if len(seen) == 1:
            chat["pick"] = "model-c"             # the user picks mid-wait
        return False
    monkeypatch.setattr(model_wait.Waiter, "wait", wait)

    def call():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise ConnectionRefusedError("down")
        return "ok"

    def ep():
        return "http://box:1234/v1", "", chat["pick"]
    assert model_wait.call_with_wait(call, endpoint=ep) == "ok"
    assert seen == ["model-a", "model-c"]


def test_the_outage_wait_probes_the_turns_failover_target(chat, monkeypatch):
    def behave(model, _):
        if model == "model-a":
            raise LLMStreamStalled("first_token", 180)
        return OK
    _fake_post(monkeypatch, chat, behave)
    _send()                                       # turn now on model-b
    assert model_failover.sticky_endpoint(_ep("model-a")).model == "model-b"
    from aiforge_core.llm import client, model_wait
    got = {}
    monkeypatch.setattr(client, "resolve", lambda role: _ep(chat["pick"]))
    monkeypatch.setattr(model_wait, "call_with_wait",
                        lambda call, endpoint, what: got.update(ep=endpoint()))
    client._waiting("chat", lambda: None)
    assert got["ep"][2] == "model-b"


def test_a_new_model_starts_with_clean_stall_bounds(chat, monkeypatch):
    scales = []

    def behave(model, _):
        if model == "model-a":
            request_health.note_stall(180)
            raise LLMStreamStalled("first_token", 180)
        scales.append((request_health.stall_scale(), request_health.stalled_seconds()))
        return OK
    _fake_post(monkeypatch, chat, behave)
    with request_health.bind(request_health.RequestHealth()):
        assert _send() == OK
    assert scales == [(1.0, 0.0)]


def test_the_extra_send_on_a_live_server_is_not_capped(chat):
    g = model_failover.begin(_ep("model-a"), _payload("model-a"), "chat")
    assert g.stall_cap() == 180
    g.alive_waits = 1
    assert g.stall_cap() == 0.0
    g.close()


def test_a_read_timeout_fails_over_at_once(chat, monkeypatch):
    def behave(model, _):
        if model == "model-a":
            raise TimeoutError("read timed out")
        return OK
    _fake_post(monkeypatch, chat, behave)
    assert _send() == OK
    assert chat["posts"] == ["model-a", "model-b"]


def test_with_no_list_the_backoff_is_one_plain_sleep(chat, monkeypatch):
    import types
    monkeypatch.setattr(chat_failover, "model_ids", lambda: [])
    slept = []
    monkeypatch.setattr(_http_retry, "time", types.SimpleNamespace(
        sleep=slept.append, monotonic=time.monotonic))

    def behave(model, _):
        raise ConnectionRefusedError("refused")
    _fake_post(monkeypatch, chat, behave)
    with pytest.raises(ConnectionRefusedError):
        _send()
    assert len(slept) == 2 and all(s >= 0.5 for s in slept)   # whole backoffs
    g = model_failover.begin(_ep("model-a"), _payload("model-a"), "chat")
    assert g.on is False and g._follows_v is None             # no follow work
    g.close()


def test_stop_ends_the_liveness_probe(monkeypatch):
    monkeypatch.setattr(model_failover, "_probe_models",
                        lambda ep, t=5.0: time.sleep(3) or True)
    t0 = time.monotonic()
    with _wait_scope.scope(lambda: True, "stopped"):
        assert model_failover._server_alive(_ep("model-a")) is False
    assert time.monotonic() - t0 < 1
