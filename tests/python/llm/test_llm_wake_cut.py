"""A machine that slept under a model call: the call is cut at the wake and
fails as a stalled stream (retried at once), instead of waiting out the
stream bounds on a dead connection."""
from __future__ import annotations

import socket
import threading
import time

import pytest

from aiforge_core.llm.client import _http as H
from aiforge_core.llm.client import _wake as W
from aiforge_core.llm.client._stream_health import LLMStreamStalled
from aiforge_core.llm.types import Endpoint


@pytest.fixture
def silent_server():
    """Accepts, reads the request, never answers."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    held = []

    def serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            held.append(c)
    threading.Thread(target=serve, daemon=True).start()
    yield srv.getsockname()[1]
    srv.close()
    for c in held:
        c.close()


def _ep(port):
    return Endpoint(base_url=f"http://127.0.0.1:{port}/v1", api_key="k",
                    model="m", provider="local", role="chat", extras={})


def _call_in_thread(port):
    box = {}

    def go():
        try:
            H._post_cancellable_once(_ep(port), b'{"model":"m"}', 60,
                                     threading.Event(), [False], None)
        except BaseException as exc:  # noqa: BLE001
            box["exc"] = exc
    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t, box


def _until(pred, s=5.0):
    end = time.monotonic() + s
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_a_wake_cuts_the_call_and_it_reads_as_a_stall(silent_server):
    t, box = _call_in_thread(silent_server)
    assert _until(lambda: bool(W._OPEN))
    time.sleep(0.2)                         # blocked waiting for the answer
    assert W.woke(900.0) == 1
    t.join(5)
    assert not t.is_alive()
    assert isinstance(box.get("exc"), LLMStreamStalled), box
    assert "slept" in str(box["exc"])
    assert W._OPEN == {}                    # untracked on the way out


def test_without_a_wake_nothing_is_cut(silent_server, monkeypatch):
    t, box = _call_in_thread(silent_server)
    assert _until(lambda: bool(W._OPEN))
    assert W.cut_by_wake(next(iter(W._OPEN))) is None
    for tok in list(W._OPEN):               # end it as a cancel would
        W._cut(W._OPEN[tok][0])
    t.join(5)
    assert not isinstance(box.get("exc"), LLMStreamStalled)


def test_zero_turns_it_off(monkeypatch):
    monkeypatch.setenv("AIFORGE_LLM_WAKE_GAP_S", "0")
    assert W.track(object()) is None
    assert W.cut_by_wake(None) is None
    W.untrack(None)


def test_the_watcher_sees_a_late_tick(monkeypatch):
    seen = []
    monkeypatch.setattr(W, "woke", lambda gap: seen.append(gap))
    clock = {"w": 1000.0, "m": 50.0}
    calls = {"n": 0}

    def fake_sleep(_s):
        calls["n"] += 1
        if calls["n"] == 2:
            clock["w"] += 1800                # the VM was paused half an hour
        clock["w"] += 0.01
        clock["m"] += 0.01
        if calls["n"] >= 4:
            raise SystemExit
    with pytest.raises(SystemExit):
        W._watch(fake_sleep, lambda: clock["w"], lambda: clock["m"])
    assert len(seen) == 1 and seen[0] > 1700


def _retry_case(monkeypatch, phase):
    import time as _t
    from aiforge_core.llm.client import _http_retry
    real = _t.monotonic
    shift = [0.0]
    calls = []

    def _post(ep, payload, timeout_s, **kw):
        calls.append(1)
        if len(calls) == 1:
            kw["sent"][0] = True
            shift[0] = 3600.0                   # slept an hour under the call
            raise LLMStreamStalled(phase, 3600)
        return {"ok": True}
    monkeypatch.setattr(H, "_post", _post)
    monkeypatch.setattr(_http_retry.time, "sleep", lambda s: None)
    monkeypatch.setattr(_http_retry.time, "monotonic", lambda: real() + shift[0])
    monkeypatch.setattr(_http_retry, "_endpoint_down", lambda ep: False)
    return calls


def test_a_call_cut_by_a_sleep_is_sent_again_whatever_the_budget(monkeypatch):
    calls = _retry_case(monkeypatch, "after_sleep")
    assert H._post_with_retry(_ep(9), b"{}", 900, role="chat",
                              source="test") == {"ok": True}
    assert len(calls) == 2


def test_an_ordinary_stall_past_the_budget_is_not(monkeypatch):
    calls = _retry_case(monkeypatch, "first_token")
    with pytest.raises(LLMStreamStalled):
        H._post_with_retry(_ep(9), b"{}", 900, role="chat", source="test")
    assert len(calls) == 1
