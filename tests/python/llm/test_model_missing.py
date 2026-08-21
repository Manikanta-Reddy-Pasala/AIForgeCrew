"""A model the endpoint does not serve is CONFIGURATION, not a transport blip.

LM Studio answers a request for an unknown model id with a 400 whose message
is model-lifecycle wording ("No models loaded…") — the same sentence it uses
for an idle-unloaded model that will JIT-reload. So the transport classifier
calls it transient and retries it, the chat loop retries five more times on top,
and the user is told "the model didn't respond", which names neither the model
nor the endpoint. One misconfigured role produced thousands of 400s that way.
"""
from __future__ import annotations
import json
import types

import pytest

from aiforge_core.llm import client as c
from aiforge_core.llm.client import _models


@pytest.fixture(autouse=True)
def _clear():
    _models.reset_cache()
    yield
    _models.reset_cache()


def _served(ids):
    """Patch the /v1/models probe to answer with `ids` (None = probe failed)."""
    class _Resp:
        def __init__(self, payload):
            self._p = payload
        def read(self):
            return json.dumps(self._p).encode()
        def __enter__(self):
            return self
        def __exit__(self, *_a):
            return False

    def _open(_req, timeout=None):
        if ids is None:
            raise OSError("probe refused")
        return _Resp({"data": [{"id": i} for i in ids]})
    return _open


def test_a_served_model_is_not_reported_missing(monkeypatch):
    monkeypatch.setattr(_models.urllib.request, "urlopen", _served(["a", "b"]))
    assert _models.model_is_missing("http://x/v1", "a") is None


def test_a_missing_model_reports_what_is_available(monkeypatch):
    monkeypatch.setattr(_models.urllib.request, "urlopen", _served(["a", "b"]))
    assert _models.model_is_missing("http://x/v1", "qwen/nope") == ["a", "b"]


def test_an_unanswerable_probe_concludes_nothing(monkeypatch):
    """No answer is not evidence. Declaring a model missing because the probe
    failed would turn every network blip into "your config is wrong"."""
    monkeypatch.setattr(_models.urllib.request, "urlopen", _served(None))
    assert _models.model_is_missing("http://x/v1", "anything") is None


def test_the_probe_is_cached(monkeypatch):
    calls = {"n": 0}
    inner = _served(["a"])

    def _counting(req, timeout=None):
        calls["n"] += 1
        return inner(req, timeout=timeout)

    monkeypatch.setattr(_models.urllib.request, "urlopen", _counting)
    for _ in range(5):
        _models.served_models("http://x/v1")
    assert calls["n"] == 1        # once per endpoint per TTL, not per failure


def _endpoint(model="qwen/nope"):
    from aiforge_core.llm.types import Endpoint
    return Endpoint(base_url="http://127.0.0.1:1234/v1", api_key="",
                    model=model, provider="openai_compatible", role="learner",
                    extras={})


def test_an_exhausted_call_names_the_model_the_endpoint_and_the_alternatives(
        monkeypatch):
    monkeypatch.setattr(_models.urllib.request, "urlopen",
                        _served(["qwen/qwen3-coder-next"]))
    monkeypatch.setattr(c, "resolve", lambda role: _endpoint())
    monkeypatch.setattr(c, "escalate", lambda *a, **k: None)
    monkeypatch.setattr(c, "fallback", lambda *a, **k: None)
    monkeypatch.setattr(c, "_try_post", lambda *a, **k: None)   # every attempt fails

    with pytest.raises(RuntimeError) as ei:
        c.complete("learner", [{"role": "user", "content": "hi"}])
    msg = str(ei.value)
    assert "llm.model_missing" in msg
    assert "qwen/nope" in msg and "127.0.0.1:1234" in msg
    assert "qwen/qwen3-coder-next" in msg          # what the box DOES serve
    assert c.model_missing(ei.value) is True


def test_a_real_transport_failure_still_reads_as_exhausted(monkeypatch):
    """The model IS served — so the failure is not config, and the message
    must not blame it."""
    monkeypatch.setattr(_models.urllib.request, "urlopen",
                        _served(["qwen/nope"]))
    monkeypatch.setattr(c, "resolve", lambda role: _endpoint())
    monkeypatch.setattr(c, "escalate", lambda *a, **k: None)
    monkeypatch.setattr(c, "fallback", lambda *a, **k: None)
    monkeypatch.setattr(c, "_try_post", lambda *a, **k: None)

    with pytest.raises(RuntimeError) as ei:
        c.complete("learner", [{"role": "user", "content": "hi"}])
    assert "llm.exhausted" in str(ei.value)
    assert c.model_missing(ei.value) is False


def test_an_unreachable_endpoint_is_not_called_a_config_error(monkeypatch):
    monkeypatch.setattr(_models.urllib.request, "urlopen", _served(None))
    monkeypatch.setattr(c, "resolve", lambda role: _endpoint())
    monkeypatch.setattr(c, "escalate", lambda *a, **k: None)
    monkeypatch.setattr(c, "fallback", lambda *a, **k: None)
    monkeypatch.setattr(c, "_try_post", lambda *a, **k: None)

    with pytest.raises(RuntimeError) as ei:
        c.complete("learner", [{"role": "user", "content": "hi"}])
    assert "llm.exhausted" in str(ei.value)
    assert c.model_missing(ei.value) is False
