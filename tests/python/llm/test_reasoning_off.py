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


def _ep(model="qwen/qwen3.8-27b", url="http://127.0.0.1:11234/v1", role="planner"):
    return Endpoint(base_url=url, api_key="x", model=model,
                    provider="openai_compatible", role=role, extras={})


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
    # the template kwarg for servers that honour it, and reasoning_effort for
    # the ones (LM Studio's qwen3.8) that only obey that
    assert llm._additional_args["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False}, "reasoning_effort": "none"}


# ── the switch that actually works: reasoning_effort ──────────────────────────
# On LM Studio, qwen3.8-27b reasons identically with enable_thinking:false and
# /no_think (measured: the same 27 reasoning chunks). Only the top-level
# reasoning_effort field stops it: 0 reasoning tokens with "none". On a coding
# task the model spent 96% of what it generated thinking and took 9x longer.

def _body(model="qwen/qwen3.8-27b", role="planner"):
    return json.loads(_http._build_body(
        _ep(model, role=role), [{"role": "user", "content": "hi"}], None, None, None, None))


def test_thinking_no_also_sends_reasoning_effort_none(registry):
    registry.append({"model": "qwen/qwen3.8-27b", "thinking": "no"})
    body = _body()
    assert body["reasoning_effort"] == "none"
    assert body["chat_template_kwargs"] == {"enable_thinking": False}     # kept for other servers


def test_only_the_planner_reasons_every_other_role_is_told_not_to(registry, monkeypatch):
    monkeypatch.delenv("AIFORGE_REASONING_ROLES", raising=False)
    monkeypatch.delenv("AIFORGE_REASONING_EFFORT", raising=False)
    for role in ("chat", "doer", "enhancer", "reviewer", "architect"):
        body = _body(role=role)
        assert body["reasoning_effort"] == "none", role
        assert body["chat_template_kwargs"] == {"enable_thinking": False}
    planner = _body(role="planner")
    assert "reasoning_effort" not in planner                               # left to the model
    assert "chat_template_kwargs" not in planner


def test_the_reasoning_roles_are_an_operator_setting(registry, monkeypatch):
    monkeypatch.setenv("AIFORGE_REASONING_ROLES", "planner, architect")
    assert reasoning.role_reasons("architect")
    assert not reasoning.role_reasons("doer")
    monkeypatch.setenv("AIFORGE_REASONING_ROLES", "")
    assert not reasoning.role_reasons("planner")                           # none may reason


def test_no_role_named_leaves_the_setting_alone(registry):
    assert reasoning.role_reasons("")
    assert not reasoning.reasoning_off("any-model")


def test_an_untouched_model_sends_nothing(registry, monkeypatch):
    monkeypatch.delenv("AIFORGE_REASONING_EFFORT", raising=False)
    registry.append({"model": "qwen/qwen3.8-27b", "thinking": "auto"})
    assert "reasoning_effort" not in _body()
    assert reasoning.effort_for("qwen/qwen3.8-27b") is None


def test_one_setting_limits_every_model_and_a_model_setting_wins(registry, monkeypatch):
    monkeypatch.setenv("AIFORGE_REASONING_EFFORT", "low")
    assert _body("whatever")["reasoning_effort"] == "low"                  # planner
    registry.append({"model": "m1", "thinking": "no"})
    assert _body("m1")["reasoning_effort"] == "none"                       # the model's own choice wins
    monkeypatch.setenv("AIFORGE_REASONING_EFFORT", "bogus")
    assert reasoning.effort_for("whatever") is None                        # unknown value: ignored


def test_a_server_that_refused_the_field_is_not_sent_it_again(registry):
    from aiforge_core.llm import fast_reasoning
    fast_reasoning.reset()
    registry.append({"model": "qwen/qwen3.8-27b", "thinking": "no"})
    assert "reasoning_effort" in _body()
    import io
    import urllib.error
    err = urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(
        b'{"error":"Unrecognized request argument supplied: reasoning_effort"}'))
    assert fast_reasoning.note_rejection("http://127.0.0.1:11234/v1", err, "qwen/qwen3.8-27b")
    body = _body()
    assert "reasoning_effort" not in body                                  # remembered
    assert body["chat_template_kwargs"] == {"enable_thinking": False}      # the older switches stay
    fast_reasoning.reset()


def test_the_team_pipeline_model_carries_it_too(registry):
    pytest.importorskip("google.adk.models.lite_llm")
    from aiforge_core.runtime.escalating_llm import _builder
    cfg = {"model_id": "openai/qwen/qwen3.8-27b",
           "api_base": "http://127.0.0.1:11234/v1", "api_key": "x"}
    doer = _builder._build_one(cfg, "doer")
    assert doer._additional_args["extra_body"]["reasoning_effort"] == "none"
    planner = _builder._build_one(cfg, "planner")
    assert "extra_body" not in planner._additional_args
