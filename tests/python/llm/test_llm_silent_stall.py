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
from aiforge_core.llm.model_outage import LLMRequestFailing, LLMStallBudget
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


def test_a_turn_past_its_stall_budget_is_a_final_llm_issue(monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_FIRST_TOKEN_S", "1")
    monkeypatch.setenv("AIFORGE_LLM_STALL_BUDGET_S", "3.5")
    request_health.bind_turn()
    # a fresh request record per send, as a fallback / persist round makes:
    # the budget is the turn's, so it still runs out
    with request_health.bind(request_health.RequestHealth()):
        with pytest.raises(LLMStreamStalled):
            _call(_serve([("sleep", 2.5)]))
    with request_health.bind(request_health.RequestHealth()):
        with pytest.raises(LLMStreamStalled):
            _call(_serve([("sleep", 2.5)]))
    with request_health.bind(request_health.RequestHealth()):
        with pytest.raises(LLMStreamStalled):
            _call(_serve([("sleep", 2.5)]))                 # 3 s of 3.5
    with request_health.bind(request_health.RequestHealth()):
        with pytest.raises(LLMStallBudget) as ei:
            _call(_serve([("sleep", 4.0)]))
    msg = str(ei.value)
    assert "never started answering" in msg and "AIFORGE_LLM_STALL_BUDGET_S" in msg
    assert msg.count("LLM issue") == 0
    from aiforge_core.llm import model_outage
    from aiforge_core.llm.retry_policy import persist_caps
    assert persist_caps(None, None, ei.value, model_outage) == (0, 0.0)


def test_a_resend_after_a_stall_is_said_in_the_chat():
    seen = []
    ep = type("E", (), {"base_url": "http://box:1234/v1"})()
    with _wait_scope.status_sink(seen.append):
        st = LLMStreamStalled("first_token", 180)
        st.budget_left = 900
        _http_retry._say_retry(ep, 1, 3, "stream_stalled", st)
        _http_retry._say_retry(ep, 1, 3, "http_400", RuntimeError("x"))
    assert len(seen) == 1
    text = seen[0]["text"]
    assert "sent nothing back for 3m" in text and "try 2 of 3" in text
    assert "waiting up to 6m" in text and "15m of this turn's stall budget left" in text


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
    monkeypatch.setenv("AIFORGE_LLM_STALL_BUDGET_S", "3.5")
    request_health.bind_turn()
    with request_health.bind(request_health.RequestHealth()):
        with pytest.raises(LLMStreamStalled):
            _call(_serve([("sleep", 2.5)]))      # 1 s
        with pytest.raises(LLMStreamStalled):
            _call(_serve([("sleep", 3.5)]))      # 2 s → 3 s
        t0 = time.monotonic()
        with pytest.raises(LLMStallBudget):
            _call(_serve([("sleep", 6.0)]))      # 4 s → clamped
        assert time.monotonic() - t0 < 1.8


def test_a_raised_read_timeout_is_never_cut_by_the_default_budget(monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_FIRST_TOKEN_S", "1")
    monkeypatch.setenv("AIFORGE_LLM_STALL_BUDGET_S", "0.5")
    monkeypatch.setenv("AIFORGE_LLM_TIMEOUT_S", "5")
    request_health.bind_turn()
    with request_health.bind(request_health.RequestHealth()):
        with pytest.raises(LLMStreamStalled):         # not the budget error
            _call(_serve([("sleep", 2.0)]), read_timeout=5)


def test_keepalive_death_is_a_dropped_connection_not_a_timeout():
    import errno
    from aiforge_core.llm.client._stream_health import StreamWatch
    w = StreamWatch(None, b"{}", 30)
    w.bound = 5.0
    out = w.stalled(TimeoutError(errno.ETIMEDOUT, "Connection timed out"))
    assert isinstance(out, ConnectionResetError)


def test_keep_alives_with_no_bound_still_end(monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_FIRST_TOKEN_S", "0")
    monkeypatch.setenv("AIFORGE_LLM_STALL_BUDGET_S", "0")
    script = [("headers", "text/event-stream")]
    script += [("send", b": keep-alive\n\n"), ("sleep", 0.3)] * 20
    with pytest.raises(LLMStreamStalled) as ei:
        _call(_serve(script), read_timeout=1)
    assert ei.value.keepalive


def test_the_idle_phase_is_never_clamped(monkeypatch):
    from aiforge_core.llm.client._stream_health import StreamWatch
    monkeypatch.setenv("AIFORGE_LLM_STALL_BUDGET_S", "10")
    request_health.bind_turn()
    request_health.note_stall(9.5)
    w = StreamWatch(None, b"{}", 0)
    w.phase = "idle"
    w._apply(120.0)
    assert w.bound == 120.0
