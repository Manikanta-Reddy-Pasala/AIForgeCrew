"""Reasoning switched off per model (Models → Thinking: no) or everywhere
(AIFORGE_NO_REASONING=1): every request carries enable_thinking:false and the
/no_think switch, on the direct client and the ADK pipeline."""
import json

import pytest

from aiforge_core.llm import reasoning
from aiforge_core.llm.client import _http
from aiforge_core.llm.types import Endpoint


@pytest.fixture
def registry(monkeypatch):
    rows: list = []
    from aiforge_core.config import model_registry as mr
    monkeypatch.setattr(mr, "_load", lambda: rows)
    monkeypatch.delenv("AIFORGE_NO_REASONING", raising=False)
    return rows


def _ep(model="qwen/qwen3.8-27b", url="http://127.0.0.1:11234/v1"):
    return Endpoint(base_url=url, api_key="x", model=model,
                    provider="openai_compatible", role="chat", extras={})


def test_thinking_no_turns_reasoning_off_for_that_model_only(registry):
    registry.append({"model": "qwen/qwen3.8-27b", "base_url": "http://127.0.0.1:11234/v1",
                     "thinking": "no"})
    assert reasoning.reasoning_off("qwen/qwen3.8-27b", "http://127.0.0.1:11234/v1")
    assert reasoning.reasoning_off("openai/qwen/qwen3.8-27b")        # LiteLLM id
    assert not reasoning.reasoning_off("other/model")


def test_the_global_switch_covers_every_model(registry, monkeypatch):
    monkeypatch.setenv("AIFORGE_NO_REASONING", "1")
    assert reasoning.reasoning_off("anything")


def test_the_request_body_carries_both_switches(registry):
    registry.append({"model": "qwen/qwen3.8-27b", "thinking": "no"})
    body = json.loads(_http._build_body(
        _ep(), [{"role": "user", "content": "hi"}], None, None, None, None))
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["messages"][-1]["content"].endswith("/no_think")


def test_a_thinking_model_is_left_alone(registry):
    registry.append({"model": "qwen/qwen3.8-27b", "thinking": "auto"})
    body = json.loads(_http._build_body(
        _ep(), [{"role": "user", "content": "hi"}], None, None, None, None))
    assert "chat_template_kwargs" not in body
    assert body["messages"][-1]["content"] == "hi"


def test_the_adk_request_gets_no_think_on_the_last_user_turn():
    pytest.importorskip("google.adk.models.llm_request")
    from google.adk.models.llm_request import LlmRequest
    from google.genai import types
    req = LlmRequest(contents=[
        types.Content(role="user", parts=[types.Part.from_text(text="first")]),
        types.Content(role="model", parts=[types.Part.from_text(text="ok")]),
        types.Content(role="user", parts=[types.Part.from_text(text="build it")]),
    ])
    out = reasoning.no_think_request(req)
    assert out.contents[-1].parts[0].text == "build it /no_think"
    assert out.contents[0].parts[0].text == "first"
    assert req.contents[-1].parts[0].text == "build it"          # original untouched


def test_the_litellm_model_is_built_with_the_kwarg(registry, monkeypatch):
    pytest.importorskip("google.adk.models.lite_llm")
    registry.append({"model": "qwen/qwen3.8-27b", "thinking": "no"})
    from aiforge_core.runtime.escalating_llm import _builder
    llm = _builder._build_one({"model_id": "openai/qwen/qwen3.8-27b",
                               "api_base": "http://127.0.0.1:11234/v1", "api_key": "x"})
    assert llm._additional_args["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
