"""Models → Thinking: auto / on / high / medium / low / off, sent the OpenAI-
compatible way. Big reasoning models (Kimi, DeepSeek) thought for minutes per
answer with no way to turn them down."""
import io
import json
import urllib.error

import pytest

from aiforge_core.llm import fast_reasoning, reasoning
from aiforge_core.llm import client as c
from aiforge_core.llm.client import _http
from aiforge_core.llm.client._http_stream import (
    REASONING_CAP_CHARS,
    ReasoningOverCap,
    _StreamAssembler,
)
from aiforge_core.llm.types import Endpoint

URL = "http://kimi.local/v1"


@pytest.fixture
def registry(monkeypatch):
    rows: list = []
    from aiforge_core.config import model_registry as mr
    monkeypatch.setattr(mr, "_load", lambda: rows)
    for k in ("AIFORGE_NO_REASONING", "AIFORGE_REASONING_EFFORT",
              "AIFORGE_THINKING_LOW_TOKENS", "AIFORGE_THINKING_MEDIUM_TOKENS"):
        monkeypatch.delenv(k, raising=False)
    fast_reasoning.reset()
    yield rows
    fast_reasoning.reset()


def _ep(model="kimi-k2.5", role="planner"):
    return Endpoint(base_url=URL, api_key="x", model=model,
                    provider="openai_compatible", role=role, extras={})


def _body(ep=None):
    return json.loads(_http._build_body(
        ep or _ep(), [{"role": "user", "content": "hi"}], None, None, None, None))


@pytest.mark.parametrize("level", ["low", "medium", "high"])
def test_a_level_is_sent_as_reasoning_effort(registry, level):
    registry.append({"model": "kimi-k2.5", "base_url": URL, "thinking": level})
    body = _body()
    assert body["reasoning_effort"] == level
    assert "thinking" not in body and "chat_template_kwargs" not in body
    assert body["messages"][-1]["content"] == "hi"


def test_off_sends_every_openai_compatible_switch(registry):
    registry.append({"model": "deepseek-v3.2", "base_url": URL, "thinking": "no"})
    body = _body(_ep("deepseek-v3.2"))
    assert body["chat_template_kwargs"] == {"enable_thinking": False, "thinking": False}
    assert body["thinking"] == {"type": "disabled"}
    assert body["reasoning_effort"] == "none"


def test_levels_count_as_a_thinking_model(registry):
    from aiforge_core.config import model_registry as mr
    row = {"id": "k", "model": "plain-name", "thinking": "low"}
    assert mr._public(row)["has_thinking"] is True
    assert mr._public({**row, "thinking": "no"})["has_thinking"] is False


def test_registry_accepts_and_rejects_values(registry):
    from aiforge_core.config import model_registry as mr
    r: dict = {}
    mr._apply_model_fields(r, {"thinking": "medium"})
    assert r["thinking"] == "medium"
    mr._apply_model_fields(r, {"thinking": "extreme"})
    assert r["thinking"] == "medium"


def test_a_role_that_may_not_reason_still_turns_it_off(registry):
    registry.append({"model": "kimi-k2.5", "base_url": URL, "thinking": "low"})
    body = _body(_ep(role="doer"))
    assert body["reasoning_effort"] == "none"


def test_cap_is_only_for_low_and_medium(registry, monkeypatch):
    registry.append({"model": "kimi-k2.5", "base_url": URL, "thinking": "low"})
    assert reasoning.cap_tokens("kimi-k2.5", URL, "planner") == 1024
    monkeypatch.setenv("AIFORGE_THINKING_LOW_TOKENS", "300")
    assert reasoning.cap_tokens("kimi-k2.5", URL, "planner") == 300
    registry[0]["thinking"] = "medium"
    assert reasoning.cap_tokens("kimi-k2.5", URL, "planner") == 4096
    for v in ("high", "yes", "no", "auto"):
        registry[0]["thinking"] = v
        assert reasoning.cap_tokens("kimi-k2.5", URL, "planner") == 0
    registry[0]["thinking"] = "low"
    with reasoning.forced_off():
        assert reasoning.cap_tokens("kimi-k2.5", URL, "planner") == 0


def _chunk(**delta):
    return {"choices": [{"delta": delta}]}


def test_stream_over_the_cap_with_no_answer_stops():
    tok = REASONING_CAP_CHARS.set(10)
    try:
        asm = _StreamAssembler(lambda *a: None)
        asm.feed(_chunk(reasoning_content="12345"))
        with pytest.raises(ReasoningOverCap):
            asm.feed(_chunk(reasoning_content="678901"))
    finally:
        REASONING_CAP_CHARS.reset(tok)


def test_stream_with_an_answer_started_is_never_cut():
    tok = REASONING_CAP_CHARS.set(10)
    try:
        asm = _StreamAssembler(lambda *a: None)
        asm.feed(_chunk(content="ok"))
        asm.feed(_chunk(reasoning_content="x" * 50))
        assert asm.body()["choices"][0]["message"]["content"] == "ok"
    finally:
        REASONING_CAP_CHARS.reset(tok)


def test_over_cap_is_not_a_transport_retry():
    from aiforge_core.llm.client._errors import is_transient_exc
    assert is_transient_exc(ReasoningOverCap("x"))[0] is False


def _refusal(msg: bytes):
    return urllib.error.HTTPError(f"{URL}/chat/completions", 400, "Bad Request", {},
                                  io.BytesIO(msg))


def _ok():
    return {"choices": [{"message": {"role": "assistant", "content": "DONE"}}]}


def test_over_cap_asks_again_with_thinking_off(registry, monkeypatch):
    registry.append({"model": "kimi-k2.5", "base_url": URL, "thinking": "low"})
    posts = []

    def _post(ep, p, t, **k):
        body = json.loads(p)
        posts.append(body)
        if len(posts) == 1:
            raise ReasoningOverCap("too long")
        return _ok()
    monkeypatch.setattr(c, "_post_with_retry", _post)
    out = c._native_post(_ep(), [{"role": "user", "content": "hi"}], None, None,
                         None, None, _http._build_body(
                             _ep(), [{"role": "user", "content": "hi"}],
                             None, None, None, None), 30, "planner", [None])
    assert out == _ok()
    assert posts[0]["reasoning_effort"] == "low"
    assert posts[1]["reasoning_effort"] == "none"
    assert posts[1]["thinking"] == {"type": "disabled"}


@pytest.mark.parametrize("msg,field", [
    (b'{"error": {"message": "Unrecognized request argument supplied: thinking"}}',
     "thinking"),
    (b'{"detail": [{"type": "extra_forbidden", "loc": ["body", "thinking"], '
     b'"msg": "Extra inputs are not permitted"}]}', "thinking"),
    (b'{"error": "unknown field chat_template_kwargs"}', "chat_template_kwargs"),
    (b'{"error": "Unrecognized request argument: reasoning_effort"}',
     "reasoning_effort"),
])
def test_each_refused_switch_is_dropped_alone(registry, monkeypatch, msg, field):
    registry.append({"model": "kimi-k2.5", "base_url": URL, "thinking": "no"})
    posts = []

    def _post(ep, p, t, **k):
        body = json.loads(p)
        posts.append(body)
        if field in body:
            raise _refusal(msg)
        return _ok()
    monkeypatch.setattr(c, "_post_with_retry", _post)
    payload = _http._build_body(_ep(), [{"role": "user", "content": "hi"}],
                                None, None, None, None)
    out = c._native_post(_ep(), [{"role": "user", "content": "hi"}], None, None,
                         None, None, payload, 30, "planner", [None])
    assert out == _ok()
    assert len(posts) == 2 and field not in posts[1]
    others = {"thinking", "chat_template_kwargs", "reasoning_effort"} - {field}
    assert others <= set(posts[1])          # the other switches still go
    assert field not in _body()             # remembered for the next request


def test_a_400_about_something_else_is_not_retried(registry, monkeypatch):
    registry.append({"model": "kimi-k2.5", "base_url": URL, "thinking": "no"})
    posts = []

    def _post(ep, p, t, **k):
        posts.append(1)
        raise _refusal(b'{"error": "maximum context length exceeded (thinking about it)"}')
    monkeypatch.setattr(c, "_post_with_retry", _post)
    payload = _http._build_body(_ep(), [{"role": "user", "content": "hi"}],
                                None, None, None, None)
    with pytest.raises(urllib.error.HTTPError):
        c._native_post(_ep(), [{"role": "user", "content": "hi"}], None, None,
                       None, None, payload, 30, "planner", [None])
    assert len(posts) == 1


def test_adk_path_sends_the_hosted_switch_only_for_an_explicit_off(registry):
    registry.append({"model": "kimi-k2.5", "base_url": URL, "thinking": "no"})
    assert reasoning.off_extras("kimi-k2.5", URL)["thinking"] == {"type": "disabled"}
    registry[0]["thinking"] = "auto"
    assert "thinking" not in reasoning.off_extras("kimi-k2.5", URL)
