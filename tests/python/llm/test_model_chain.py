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


def test_embedding_models_are_never_tried(monkeypatch):
    """An embedding model cannot answer a chat turn; trying it spends a round
    trip to be told so."""
    tried = _wire(monkeypatch, {})
    with pytest.raises(RuntimeError):
        c.complete("chat", [{"role": "user", "content": "q"}])
    assert not any("embed" in t[0] for t in tried)


def test_every_configured_model_failing_still_reports_the_real_error(monkeypatch):
    tried = _wire(monkeypatch, {})
    with pytest.raises(RuntimeError) as ei:
        c.complete("chat", [{"role": "user", "content": "q"}])
    assert "llm.exhausted" in str(ei.value) or "llm.model_missing" in str(ei.value)
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
