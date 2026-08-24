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


def _fails_with(exc):
    """A _try_post that fails the way `exc` says, recording it like the real one
    (the diagnosis is gated on WHAT killed the chain)."""
    def fn(*_a, shipped=None, **_k):
        if shipped is not None:
            shipped["exc"] = exc
        return None
    return fn


def _http_400():
    import io
    import urllib.error
    return urllib.error.HTTPError(
        "http://x/v1/chat/completions", 400, "Bad Request", None,
        io.BytesIO(b'{"error": {"message": "No models loaded."}}'))


def test_an_exhausted_call_names_the_model_the_endpoint_and_the_alternatives(
        monkeypatch):
    monkeypatch.setattr(_models.urllib.request, "urlopen",
                        _served(["qwen/qwen3-coder-next"]))
    monkeypatch.setattr(c, "resolve", lambda role: _endpoint())
    monkeypatch.setattr(c, "escalate", lambda *a, **k: None)
    monkeypatch.setattr(c, "fallback", lambda *a, **k: None)
    monkeypatch.setattr(c, "_try_post", _fails_with(_http_400()))

    with pytest.raises(RuntimeError) as ei:
        c.complete("learner", [{"role": "user", "content": "hi"}])
    msg = str(ei.value)
    assert "llm.model_missing" in msg
    assert "qwen/nope" in msg
    assert "127.0.0.1:1234" in msg
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
    monkeypatch.setattr(c, "_try_post", _fails_with(_http_400()))

    with pytest.raises(RuntimeError) as ei:
        c.complete("learner", [{"role": "user", "content": "hi"}])
    assert "llm.exhausted" in str(ei.value)
    assert c.model_missing(ei.value) is False


def test_an_unreachable_endpoint_is_not_called_a_config_error(monkeypatch):
    monkeypatch.setattr(_models.urllib.request, "urlopen", _served(None))
    monkeypatch.setattr(c, "resolve", lambda role: _endpoint())
    monkeypatch.setattr(c, "escalate", lambda *a, **k: None)
    monkeypatch.setattr(c, "fallback", lambda *a, **k: None)
    monkeypatch.setattr(c, "_try_post", _fails_with(_http_400()))

    with pytest.raises(RuntimeError) as ei:
        c.complete("learner", [{"role": "user", "content": "hi"}])
    assert "llm.exhausted" in str(ei.value)
    assert c.model_missing(ei.value) is False


def test_a_shipped_timeout_is_never_renamed_a_missing_model(monkeypatch):
    """A read timeout means the box ACCEPTED the prompt and is still
    generating — proof the model exists. Renaming it "your config is wrong"
    buries the one failure the no-re-POST rule is built around, and the marker
    that stops the layer above from re-issuing it."""
    probed = {"n": 0}

    def _count(*_a, **_k):
        probed["n"] += 1
        raise AssertionError("must not probe")

    monkeypatch.setattr(_models.urllib.request, "urlopen", _count)
    monkeypatch.setattr(c, "resolve", lambda role: _endpoint())
    monkeypatch.setattr(c, "escalate", lambda *a, **k: None)
    monkeypatch.setattr(c, "fallback", lambda *a, **k: None)

    def _timeout(*_a, shipped=None, **_k):
        if shipped is not None:
            shipped["timeout"] = True
            shipped["exc"] = TimeoutError("read timed out")
        return None

    monkeypatch.setattr(c, "_try_post", _timeout)
    with pytest.raises(RuntimeError) as ei:
        c.complete("learner", [{"role": "user", "content": "hi"}])
    assert "llm.exhausted" in str(ei.value)
    assert probed["n"] == 0


def test_an_unreachable_box_is_not_probed(monkeypatch):
    """A refused connection says nothing about model configuration, and the
    diagnosis must not send an outbound request on every unrelated failure."""
    probed = {"n": 0}

    def _count(*_a, **_k):
        probed["n"] += 1
        raise AssertionError("must not probe")

    monkeypatch.setattr(_models.urllib.request, "urlopen", _count)
    monkeypatch.setattr(c, "resolve", lambda role: _endpoint())
    monkeypatch.setattr(c, "escalate", lambda *a, **k: None)
    monkeypatch.setattr(c, "fallback", lambda *a, **k: None)
    monkeypatch.setattr(c, "_try_post",
                        _fails_with(ConnectionRefusedError("nope")))
    with pytest.raises(RuntimeError) as ei:
        c.complete("learner", [{"role": "user", "content": "hi"}])
    assert "llm.exhausted" in str(ei.value)
    assert probed["n"] == 0


# ── standing in for a model the box does not have ────────────────────────


def test_the_substitute_is_the_closest_id_the_box_serves():
    from aiforge_core.llm.client._models import pick_substitute as pick
    served = ["llama-3.3-70b", "qwen/qwen3-coder-next", "mistral-small"]
    assert pick("qwen/qwen3.6-27b", served) == "qwen/qwen3-coder-next"
    assert pick("anything", []) is None


def test_the_substitute_is_deterministic():
    """A stand-in that moves around is worse than one that is imperfect —
    nobody can reproduce a bug that ran on a different model each time."""
    from aiforge_core.llm.client._models import pick_substitute as pick
    served = ["qwen/a-1", "qwen/a-2", "qwen/b-1"]
    picks = {pick("qwen/a-9", list(reversed(served))) for _ in range(5)}
    picks |= {pick("qwen/a-9", served) for _ in range(5)}
    assert len(picks) == 1


def test_a_missing_model_falls_back_to_one_that_is_served(monkeypatch):
    monkeypatch.delenv("AIFORGE_LLM_MODEL_AUTOFALLBACK", raising=False)
    monkeypatch.setattr(_models.urllib.request, "urlopen",
                        _served(["qwen/qwen3-coder-next"]))
    monkeypatch.setattr(c, "resolve", lambda role: _endpoint())
    monkeypatch.setattr(c, "escalate", lambda *a, **k: None)
    monkeypatch.setattr(c, "fallback", lambda *a, **k: None)

    seen: list = []
    fail = _fails_with(_http_400())

    def _post(ep, messages, **kw):
        seen.append((ep.model, kw.get("source")))
        if ep.model == "qwen/qwen3-coder-next":
            return ("rescued", {})
        return fail(ep, messages, **kw)

    monkeypatch.setattr(c, "_try_post", _post)
    assert c.complete("learner", [{"role": "user", "content": "hi"}]) == "rescued"
    assert ("qwen/qwen3-coder-next", "model_substitute") in seen


def test_the_fallback_can_be_turned_off(monkeypatch):
    """An operator whose model choice IS the experiment wants a wrong model to
    be a hard failure, not a quiet substitution."""
    monkeypatch.setenv("AIFORGE_LLM_MODEL_AUTOFALLBACK", "0")
    monkeypatch.setattr(_models.urllib.request, "urlopen",
                        _served(["qwen/qwen3-coder-next"]))
    monkeypatch.setattr(c, "resolve", lambda role: _endpoint())
    monkeypatch.setattr(c, "escalate", lambda *a, **k: None)
    monkeypatch.setattr(c, "fallback", lambda *a, **k: None)
    monkeypatch.setattr(c, "_try_post", _fails_with(_http_400()))
    with pytest.raises(RuntimeError) as ei:
        c.complete("learner", [{"role": "user", "content": "hi"}])
    assert "llm.model_missing" in str(ei.value)


def test_a_substitute_that_also_fails_still_names_the_real_problem(monkeypatch):
    """The rescue is best-effort. When it does not work the operator must still
    be told which model was configured and what the box actually has."""
    monkeypatch.delenv("AIFORGE_LLM_MODEL_AUTOFALLBACK", raising=False)
    monkeypatch.setattr(_models.urllib.request, "urlopen",
                        _served(["qwen/qwen3-coder-next"]))
    monkeypatch.setattr(c, "resolve", lambda role: _endpoint())
    monkeypatch.setattr(c, "escalate", lambda *a, **k: None)
    monkeypatch.setattr(c, "fallback", lambda *a, **k: None)
    monkeypatch.setattr(c, "_try_post", _fails_with(_http_400()))
    with pytest.raises(RuntimeError) as ei:
        c.complete("learner", [{"role": "user", "content": "hi"}])
    msg = str(ei.value)
    assert "llm.model_missing" in msg
    assert "qwen/qwen3-coder-next" in msg


def test_an_empty_served_list_is_not_a_substitution(monkeypatch):
    """"The box serves nothing" is not a menu to pick from."""
    monkeypatch.setattr(_models.urllib.request, "urlopen", _served([]))
    monkeypatch.setattr(c, "resolve", lambda role: _endpoint())
    monkeypatch.setattr(c, "escalate", lambda *a, **k: None)
    monkeypatch.setattr(c, "fallback", lambda *a, **k: None)
    monkeypatch.setattr(c, "_try_post", _fails_with(_http_400()))
    with pytest.raises(RuntimeError) as ei:
        c.complete("learner", [{"role": "user", "content": "hi"}])
    assert "none loaded" in str(ei.value)
