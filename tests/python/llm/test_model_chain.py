"""When the selected model stops answering, try the other configured ones.

"I added four models; chat picks one, and if that model fails it should go to
the others." Until now the registry was a SELECTION list only: the provider
fallback chain is for cloud escalation and is empty without a cloud key, so on
a single-provider install a dead model was the end of the road.
"""
from __future__ import annotations

import pytest

from aiforge_core.config import model_registry
from aiforge_core.llm import client as c


@pytest.fixture(autouse=True)
def _registry(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("AIFORGE_LLM_MODEL_CHAIN", raising=False)
    rows = [
        {"id": "a", "model": "qwen/first", "base_url": "", "api_key": ""},
        {"id": "b", "model": "qwen/second", "base_url": "", "api_key": ""},
        {"id": "c", "model": "qwen/third", "base_url": "http://other:1234/v1",
         "api_key": "sk-other"},
        {"id": "d", "model": "text-embed-small", "base_url": "", "api_key": ""},
        {"id": "e", "model": "bge-reranker-v2-m3", "base_url": "",
         "api_key": ""},
    ]
    monkeypatch.setattr(model_registry, "_load", lambda: [dict(r) for r in rows])
    yield rows


def _ep(model="qwen/first"):
    from aiforge_core.llm.types import Endpoint
    return Endpoint(base_url="http://127.0.0.1:1234/v1", api_key="k",
                    model=model, provider="openai_compatible", role="chat",
                    extras={})


def _wire(monkeypatch, answers: dict):
    """`answers` maps model id -> text, or None to make that model fail."""
    tried: list = []

    def _try_post(ep, messages, **kw):
        tried.append((ep.model, ep.base_url, ep.api_key))
        got = answers.get(ep.model)
        return (got, {}) if got else None

    monkeypatch.setattr(c, "resolve", lambda role: _ep())
    monkeypatch.setattr(c, "escalate", lambda *a, **k: None)
    monkeypatch.setattr(c, "fallback", lambda *a, **k: None)
    monkeypatch.setattr(c, "_try_post", _try_post)
    return tried


def test_the_others_are_tried_in_order(monkeypatch):
    tried = _wire(monkeypatch, {"qwen/second": "answered"})
    assert c.complete("chat", [{"role": "user", "content": "q"}]) == "answered"
    assert [t[0] for t in tried] == ["qwen/first", "qwen/second"]


def test_the_dead_model_is_not_offered_back_to_itself(monkeypatch):
    tried = _wire(monkeypatch, {"qwen/third": "answered"})
    c.complete("chat", [{"role": "user", "content": "q"}])
    assert [t[0] for t in tried].count("qwen/first") == 1


def test_a_row_with_its_own_host_carries_its_own_key(monkeypatch):
    """A configured model may live on a different box. Sending the first
    endpoint's key to it is an auth failure dressed as a dead model."""
    tried = _wire(monkeypatch, {"qwen/third": "answered"})
    assert c.complete("chat", [{"role": "user", "content": "q"}]) == "answered"
    assert ("qwen/third", "http://other:1234/v1", "sk-other") in tried


def test_a_same_host_row_keeps_the_endpoint(monkeypatch):
    tried = _wire(monkeypatch, {"qwen/second": "answered"})
    c.complete("chat", [{"role": "user", "content": "q"}])
    assert ("qwen/second", "http://127.0.0.1:1234/v1", "k") in tried


def test_non_generative_models_are_never_tried(monkeypatch):
    """An embedding or reranker model cannot answer a chat turn; trying one
    spends a full round trip (plus its empty-retries) to be told so. A
    substring check for "embed" let every bge-/gte-/e5- reranker through — the
    registry already has the real test."""
    tried = _wire(monkeypatch, {})
    with pytest.raises(RuntimeError):
        c.complete("chat", [{"role": "user", "content": "q"}])
    names = [t[0] for t in tried]
    assert not any("embed" in n for n in names)
    assert "bge-reranker-v2-m3" not in names


def test_the_same_model_on_a_SECOND_host_is_still_tried(monkeypatch):
    """Two boxes serving the same model is the textbook redundancy setup, and
    the best fallback there is. Excluding by model NAME threw away the healthy
    copy along with the dead one."""
    monkeypatch.setattr(model_registry, "_load", lambda: [
        {"id": "a", "model": "qwen/first", "base_url": "", "api_key": ""},
        {"id": "b", "model": "qwen/first", "base_url": "http://spare:1234/v1",
         "api_key": "sk-spare"},
    ])
    tried = _wire(monkeypatch, {})
    with pytest.raises(RuntimeError):
        c.complete("chat", [{"role": "user", "content": "q"}])
    assert ("qwen/first", "http://spare:1234/v1", "sk-spare") in tried


def test_a_keyless_row_on_another_host_gets_NO_key(monkeypatch):
    """The primary's credential must not travel to a host it was never issued
    for — on a LAN box that means a cloud key in plaintext on the wire and in
    that box's request log."""
    monkeypatch.setattr(model_registry, "_load", lambda: [
        {"id": "a", "model": "qwen/first", "base_url": "", "api_key": ""},
        {"id": "b", "model": "qwen/lan", "base_url": "http://192.168.1.50:1234/v1",
         "api_key": ""},
    ])
    tried = _wire(monkeypatch, {})
    with pytest.raises(RuntimeError):
        c.complete("chat", [{"role": "user", "content": "q"}])
    lan = [t for t in tried if t[1] == "http://192.168.1.50:1234/v1"]
    assert lan and lan[0][2] == "", f"primary key leaked: {lan}"


def test_a_lan_insecure_flag_does_not_follow_to_another_host(monkeypatch):
    """`insecure_tls` rides in extras. Inherited, an operator's "skip TLS
    verify" for their LAN box silently strips verification from every other
    configured endpoint, public ones included."""
    from aiforge_core.llm.types import Endpoint
    seen: list = []

    def _try_post(ep, messages, **kw):
        seen.append((ep.base_url, dict(ep.extras or {})))
        return None

    monkeypatch.setattr(model_registry, "_load", lambda: [
        {"id": "a", "model": "qwen/first", "base_url": "", "api_key": ""},
        {"id": "b", "model": "gpt-x", "base_url": "https://api.vendor.com/v1",
         "api_key": "sk-v", "insecure_tls": False},
    ])
    monkeypatch.setattr(c, "resolve", lambda role: Endpoint(
        base_url="http://127.0.0.1:1234/v1", api_key="k", model="qwen/first",
        provider="openai_compatible", role="chat",
        extras={"insecure_tls": True}))          # the LAN opt-out
    monkeypatch.setattr(c, "escalate", lambda *a, **k: None)
    monkeypatch.setattr(c, "fallback", lambda *a, **k: None)
    monkeypatch.setattr(c, "_try_post", _try_post)
    with pytest.raises(RuntimeError):
        c.complete("chat", [{"role": "user", "content": "q"}])
    vendor = [x for x in seen if x[0] == "https://api.vendor.com/v1"]
    assert vendor and not vendor[0][1].get("insecure_tls"), seen


def test_a_vision_request_never_falls_through(monkeypatch):
    """The vision role was chosen BECAUSE it can see. Re-uploading megabytes of
    base64 to text-only models wastes the upload — and a server that silently
    drops the image block answers with a plausible caption of an image it never
    saw."""
    tried = _wire(monkeypatch, {"qwen/second": "a cat, probably"})
    with pytest.raises(RuntimeError):
        c.complete("chat", [{"role": "user", "content": [
            {"type": "text", "text": "what is this?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]}])
    assert [t[0] for t in tried] == ["qwen/first"]


def test_a_shipped_timeout_is_not_re_issued_across_the_chain(monkeypatch):
    """A read timeout means the model RECEIVED the prompt and is still working.
    Every layer refuses to re-issue it; the chain must not be the one place
    that does it N more times."""
    tried: list = []

    def _try_post(ep, messages, *, shipped=None, **kw):
        tried.append(ep.model)
        if shipped is not None:
            shipped["timeout"] = True
        return None

    monkeypatch.setattr(c, "resolve", lambda role: _ep())
    monkeypatch.setattr(c, "escalate", lambda *a, **k: None)
    monkeypatch.setattr(c, "fallback", lambda *a, **k: None)
    monkeypatch.setattr(c, "_try_post", _try_post)
    with pytest.raises(RuntimeError):
        c.complete("chat", [{"role": "user", "content": "q"}])
    assert tried == ["qwen/first"]


def test_a_context_overflow_escalation_skips_the_chain(monkeypatch):
    """The prompt was escalated because it does NOT fit locally. Chaining from
    the escalated endpoint would offer the failed local model back to itself
    and re-send an oversized prompt to every local model."""
    from aiforge_core.llm.types import Endpoint
    cloud = Endpoint(base_url="https://api.vendor.com/v1", api_key="sk-cloud",
                     model="gpt-big", provider="openai_compatible",
                     role="chat", extras={})
    tried = _wire(monkeypatch, {})
    monkeypatch.setattr(c, "escalate",
                        lambda role, reason=None, **k:
                        cloud if reason == "context_overflow" else None)
    with pytest.raises(RuntimeError):
        c.complete("chat", [{"role": "user", "content": "q"}])
    assert [t[0] for t in tried] == ["gpt-big"], tried


def test_every_configured_model_failing_still_reports_the_real_error(monkeypatch):
    tried = _wire(monkeypatch, {})
    with pytest.raises(RuntimeError) as ei:
        c.complete("chat", [{"role": "user", "content": "q"}])
    # The error must SAY the chain was walked — "llm.exhausted" alone is what
    # this path raises with or without a chain, so asserting that proves
    # nothing about the chain.
    assert "configured model(s)" in str(ei.value), str(ei.value)
    assert [t[0] for t in tried] == ["qwen/first", "qwen/second", "qwen/third"]


def test_the_chain_can_be_turned_off(monkeypatch):
    """Off is the right setting when a run must be attributable to one exact
    model."""
    monkeypatch.setenv("AIFORGE_LLM_MODEL_CHAIN", "0")
    tried = _wire(monkeypatch, {"qwen/second": "answered"})
    with pytest.raises(RuntimeError):
        c.complete("chat", [{"role": "user", "content": "q"}])
    assert [t[0] for t in tried] == ["qwen/first"]


def test_a_working_model_never_reaches_the_chain(monkeypatch):
    tried = _wire(monkeypatch, {"qwen/first": "answered"})
    assert c.complete("chat", [{"role": "user", "content": "q"}]) == "answered"
    assert [t[0] for t in tried] == ["qwen/first"]


def test_a_user_stop_does_not_become_more_traffic(monkeypatch):
    """Stop must end the call, not start N more. Every earlier test stubbed
    `_try_post` wholesale, so a mutation that swallowed cancel and walked the
    chain passed the whole file."""
    from aiforge_core.llm.client._errors import _LLMCancelled

    tried: list = []

    def _try_post(ep, messages, **kw):
        tried.append(ep.model)
        raise _LLMCancelled("stopped by user")

    monkeypatch.setattr(c, "resolve", lambda role: _ep())
    monkeypatch.setattr(c, "escalate", lambda *a, **k: None)
    monkeypatch.setattr(c, "fallback", lambda *a, **k: None)
    monkeypatch.setattr(c, "_try_post", _try_post)
    with pytest.raises(_LLMCancelled):
        c.complete("chat", [{"role": "user", "content": "q"}])
    assert tried == ["qwen/first"]


def test_the_chain_runs_after_the_provider_fallback_not_before(monkeypatch):
    """"Deliberately AFTER the same-provider fallback": a chain that jumped the
    queue would answer from a different MODEL when the operator's configured
    cloud fallback for the SAME model was available."""
    from aiforge_core.llm.types import Endpoint
    order: list = []

    # A DIFFERENT provider: complete() only tries fallback() when it is one
    # (a same-provider "fallback" is the endpoint that just failed).
    fb = Endpoint(base_url="https://fallback.example/v1", api_key="fk",
                  model="qwen/first", provider="anthropic",
                  role="chat", extras={})

    def _try_post(ep, messages, **kw):
        order.append((kw.get("source"), ep.model))
        return ("from the provider fallback", {}) if kw.get(
            "source") == "fallback" else None

    monkeypatch.setattr(c, "resolve", lambda role: _ep())
    monkeypatch.setattr(c, "escalate", lambda *a, **k: None)
    monkeypatch.setattr(c, "fallback", lambda role: fb)
    monkeypatch.setattr(c, "_try_post", _try_post)
    assert c.complete("chat", [{"role": "user", "content": "q"}]) == \
        "from the provider fallback"
    assert [o[0] for o in order] == ["primary", "fallback"]


def test_each_chain_model_gets_ONE_post_not_the_empty_ladder(monkeypatch):
    """The empty-retry ladder belongs to the model the operator CHOSE.
    Multiplied across four configured models it turned one message into 16+
    full generations, and the chat loop then re-issues the whole call."""
    seen: list = []

    def _try_post(ep, messages, **kw):
        seen.append((ep.model, kw.get("empty_retries")))
        return None

    monkeypatch.setattr(c, "resolve", lambda role: _ep())
    monkeypatch.setattr(c, "escalate", lambda *a, **k: None)
    monkeypatch.setattr(c, "fallback", lambda *a, **k: None)
    monkeypatch.setattr(c, "_try_post", _try_post)
    with pytest.raises(RuntimeError):
        c.complete("chat", [{"role": "user", "content": "q"}])
    chain = [s for s in seen if s[0] != "qwen/first"]
    assert chain and all(s[1] == 0 for s in chain), seen


def test_a_malformed_registry_row_does_not_kill_the_chain(monkeypatch):
    """One bad row used to take every other model down with it (the blanket
    except disabled the whole chain), or crash complete() with an AttributeError
    from inside the LLM client."""
    monkeypatch.setattr(model_registry, "_load", lambda: [
        {"id": "a", "model": "qwen/first", "base_url": "", "api_key": ""},
        "not-a-dict-at-all",
        {"id": "b", "model": "qwen/second", "base_url": 1234, "api_key": ""},
        {"id": "c", "model": "qwen/third", "base_url": "", "api_key": ""},
    ])
    tried = _wire(monkeypatch, {"qwen/third": "answered"})
    assert c.complete("chat", [{"role": "user", "content": "q"}]) == "answered"
    assert "qwen/third" in [t[0] for t in tried]


def test_a_missing_model_is_still_REPORTED_when_the_chain_rescues_it(monkeypatch):
    """A rescue is not a fix. If the chain quietly answers, every turn is
    served by a model the operator did not select — at WARNING level only,
    forever — and the config error is never surfaced."""
    from aiforge_core.llm.client import _models

    _models.reset_cache()
    monkeypatch.setattr(_models, "served_models",
                        lambda *a, **k: ["qwen/second"])
    tried = _wire(monkeypatch, {"qwen/second": "answered"})

    def _fails(ep, messages, *, shipped=None, **kw):
        tried.append((ep.model, ep.base_url, ep.api_key))
        if ep.model == "qwen/second":
            return ("answered", {})
        if shipped is not None:
            import urllib.error
            shipped["exc"] = urllib.error.HTTPError(
                "u", 404, "model not found", None, None)
        return None

    monkeypatch.setattr(c, "_try_post", _fails)
    # Record through the module's own logger object rather than caplog: another
    # test in this directory can leave `propagate` off on that logger, and the
    # assertion would then fail for a reason unrelated to what it tests.
    logged: list = []

    class _Rec:
        def error(self, msg, *a, **k):
            logged.append(str(msg) % a if a else str(msg))
        def warning(self, *a, **k):
            pass
        def info(self, *a, **k):
            pass
        def debug(self, *a, **k):
            pass

    monkeypatch.setattr(c, "_log", _Rec())
    assert c.complete("chat", [{"role": "user", "content": "q"}]) == "answered"
    assert any("llm.model_missing" in m for m in logged), \
        f"the config error must be reported even when a fallback answers: {logged}"
    _models.reset_cache()


def test_the_native_tool_path_has_the_chain_too(monkeypatch):
    """AIFORGE_CHAT_TOOL_PROTOCOL defaults to "native", so SIMPLE CHAT comes
    through complete_raw. A chain that only existed on the other path was one
    the user could never reach — which is the whole feature."""
    from aiforge_core.llm.types import Endpoint
    import json as _json

    seen: list = []

    def _post_with_retry(ep, payload, timeout_s, *, role, source, meter=None):
        body = _json.loads(payload.decode())
        seen.append((body.get("model"), ep.base_url, ep.api_key))
        if body.get("model") != "qwen/second":
            raise urllib_error_url("dead")
        return {"choices": [{"message": {"role": "assistant",
                                         "content": "native answer"}}]}

    monkeypatch.setattr(c, "resolve", lambda role: Endpoint(
        base_url="http://127.0.0.1:1234/v1", api_key="k", model="qwen/first",
        provider="openai_compatible", role="chat", extras={}))
    monkeypatch.setattr(c, "_post_with_retry", _post_with_retry)
    monkeypatch.setattr(c, "_record_usage", lambda *a, **k: None)
    out = c.complete_raw("chat", [{"role": "user", "content": "q"}])
    assert out.get("content") == "native answer"
    assert [s[0] for s in seen] == ["qwen/first", "qwen/second"]


def urllib_error_url(msg):
    import urllib.error
    return urllib.error.URLError(msg)
