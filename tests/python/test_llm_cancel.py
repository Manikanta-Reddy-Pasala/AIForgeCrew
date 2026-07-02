"""The opt-in cancellable HTTP path: when a cancel event is bound for the
thread, _post aborts instead of issuing/awaiting the request. The default
(no token) path stays urllib — verified untouched by the rest of the suite."""
import threading

import pytest

from aiforge_core.llm import client as c
from aiforge_core.llm.types import Endpoint


def _ep():
    return Endpoint(base_url="http://127.0.0.1:9", api_key="x", model="m",
                    provider="openai_compatible", role="doer", extras={})


def test_post_aborts_when_cancel_already_set():
    ev = threading.Event()
    ev.set()                       # Stop already pressed
    c.set_cancel_event(ev)
    try:
        with pytest.raises(c._LLMCancelled):
            c._post(_ep(), b"{}", 5)
    finally:
        c.set_cancel_event(None)


def test_cancelled_exc_is_non_retryable():
    retry, label = c._is_transient_exc(c._LLMCancelled("x"))
    assert retry is False and label == "cancelled"


def test_no_token_uses_default_path(monkeypatch):
    # With no cancel token bound, _post must take the urllib path. Stub urlopen
    # to prove it's used (and the cancellable http.client path is NOT).
    # Disable the connect-preflight so this transport-stubbed test doesn't do a
    # real TCP probe to the fake endpoint.
    monkeypatch.setenv("AIFORGE_LLM_CONNECT_TIMEOUT_S", "0")
    c.set_cancel_event(None)
    called = {}

    class _Resp:
        def read(self): return b'{"choices":[{"message":{"content":"hi"}}]}'
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(c.urllib.request, "urlopen", lambda *a, **k: _Resp())
    monkeypatch.setattr(c._rl, "acquire", lambda *a, **k: None)
    out = c._post(_ep(), b"{}", 5)
    assert out["choices"][0]["message"]["content"] == "hi"
