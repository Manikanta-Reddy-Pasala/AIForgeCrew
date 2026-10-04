"""A reasoning phase is bounded: past its budget, or when it ends with thoughts
and no answer, the same request is asked again without reasoning — once — so
the stage always answers. (Live: a Planner that thought up to the reply cap
took 285 s and left an empty plan, twice in one run.)"""
from __future__ import annotations

import asyncio

import pytest
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types as gtypes

from aiforge_core.llm import reasoning
from aiforge_core.runtime.escalating_llm import EscalatingLlm, _builder


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("AIFORGE_REASONING_BUDGET_TOKENS", "AIFORGE_REASONING_ROLES",
              "AIFORGE_NO_REASONING", "AIFORGE_REASONING_EFFORT",
              "AIFORGE_LLM_MAX_TOKENS"):
        monkeypatch.delenv(k, raising=False)
    from aiforge_core.config import model_registry
    monkeypatch.setattr(model_registry, "_load", lambda: [])


def _thought(text, partial=True):
    return LlmResponse(partial=partial, content=gtypes.Content(
        role="model", parts=[gtypes.Part(text=text, thought=True)]))


def _text(text, partial=False):
    return LlmResponse(partial=partial, content=gtypes.Content(
        role="model", parts=[gtypes.Part.from_text(text=text)]))


class _Stub(BaseLlm):
    script: list = []
    calls: int = 0
    sent: int = 0            # chunks actually pulled by the consumer
    last_user: str = ""
    seen: list = []          # (role, text) of the last request's contents

    async def generate_content_async(self, llm_request, stream=False):
        self.calls += 1
        self.seen = [(c.role, c.parts[0].text) for c in llm_request.contents or []]
        for c in reversed(llm_request.contents or []):
            if c.role == "user":
                self.last_user = c.parts[0].text
                break
        for r in self.script:
            self.sent += 1
            yield r

    @classmethod
    def supported_models(cls):
        return []


def _run(primary, plain=None):
    e = EscalatingLlm(model="primary", role="planner", primary_model=primary,
                      plain_model=plain, chain_models=[], chain_labels=[])
    req = LlmRequest(model="primary", contents=[gtypes.Content(
        role="user", parts=[gtypes.Part.from_text(text="plan it")])])

    async def _go():
        return [r async for r in e.generate_content_async(req, stream=True)]
    return asyncio.run(_go())


def _answers(out):
    return [p.text for r in out if not r.partial
            for p in r.content.parts if not p.thought]


# ── the budget ─────────────────────────────────────────────────────────

def test_the_default_budget_is_a_quarter_of_the_reply_cap(monkeypatch):
    from aiforge_core.config import runtime_settings
    monkeypatch.setattr(runtime_settings, "get",
                        lambda key: 8192 if key == "max_output_tokens" else None)
    assert reasoning.budget_tokens() == 2048


def test_the_budget_is_set_by_env_and_zero_means_no_limit(monkeypatch):
    monkeypatch.setenv("AIFORGE_REASONING_BUDGET_TOKENS", "1500")
    assert reasoning.budget_tokens() == 1500
    monkeypatch.setenv("AIFORGE_REASONING_BUDGET_TOKENS", "0")
    assert reasoning.budget_tokens() == 0


# ── the stream ─────────────────────────────────────────────────────────

def test_reasoning_past_the_budget_is_cut_and_answered_without_it(monkeypatch):
    monkeypatch.setenv("AIFORGE_REASONING_BUDGET_TOKENS", "10")     # 40 chars
    primary = _Stub(model="primary",
                    script=[_thought("x" * 25) for _ in range(50)])
    plain = _Stub(model="primary", script=[_text("the plan")])
    out = _run(primary, plain)
    assert _answers(out) == ["the plan"]
    assert primary.calls == 1 and plain.calls == 1
    assert primary.sent == 2            # stopped at the budget, not at chunk 50
    assert plain.last_user.endswith("/no_think")


def test_the_reasoning_done_so_far_is_kept_as_notes_for_the_answer(monkeypatch):
    from aiforge_core.runtime.escalating_llm._streaming import WRAP_UP
    monkeypatch.setenv("AIFORGE_REASONING_BUDGET_TOKENS", "10")     # 40 chars
    primary = _Stub(model="primary", script=[
        _thought("first the cache, "), _thought("then the tests; ordering matters"),
        _thought("never reached")])
    plain = _Stub(model="primary", script=[_text("the plan")])
    assert _answers(_run(primary, plain)) == ["the plan"]
    assert plain.seen[0] == ("user", "plan it")
    role, notes = plain.seen[1]
    assert role == "model" and notes.endswith("then the tests; ordering matters")
    assert "never reached" not in notes
    assert plain.seen[2] == ("user", WRAP_UP + " /no_think")


def test_a_reply_that_ends_in_thoughts_alone_is_asked_again(monkeypatch):
    """The reply cap ran out mid-thought: the finished response carries only
    reasoning. It must not become the stage's (empty) answer."""
    monkeypatch.setenv("AIFORGE_REASONING_BUDGET_TOKENS", "1000")
    primary = _Stub(model="primary", script=[
        _thought("thinking"), _thought("thinking", partial=False)])
    plain = _Stub(model="primary", script=[_text("the plan")])
    out = _run(primary, plain)
    assert _answers(out) == ["the plan"]
    assert not any(p.thought for r in out if not r.partial
                   for p in r.content.parts)
    assert plain.calls == 1


def test_reasoning_within_the_budget_is_left_alone(monkeypatch):
    monkeypatch.setenv("AIFORGE_REASONING_BUDGET_TOKENS", "1000")
    script = [_thought("short thought"), _text("the ", partial=True),
              _text("the plan")]
    primary = _Stub(model="primary", script=script)
    plain = _Stub(model="primary", script=[_text("never asked")])
    out = _run(primary, plain)
    assert out == script                 # same responses, same order
    assert plain.calls == 0


def test_the_plain_retry_happens_once(monkeypatch):
    monkeypatch.setenv("AIFORGE_REASONING_BUDGET_TOKENS", "1000")
    primary = _Stub(model="primary", script=[_thought("t", partial=False)])
    plain = _Stub(model="primary", script=[_thought("t", partial=False)])
    out = _run(primary, plain)
    assert primary.calls == 1 and plain.calls == 1
    assert len(out) == 1                 # what the twin said is passed on as is


def test_without_a_twin_the_stream_is_untouched():
    script = [_thought("t" * 100000), _thought("t", partial=False)]
    primary = _Stub(model="primary", script=script)
    assert _run(primary) == script


# ── the twin ───────────────────────────────────────────────────────────

_CFG = {"model_id": "openai/some-model", "api_base": "http://127.0.0.1:9/v1",
        "api_key": "x"}


def test_the_twin_is_the_same_model_with_reasoning_off():
    pytest.importorskip("google.adk.models.lite_llm")
    twin = _builder._build_one(_CFG, "planner", reasoning=False)
    body = twin._additional_args["extra_body"]
    assert body["reasoning_effort"] == "none"
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert twin.model == _CFG["model_id"]


def test_only_a_role_that_reasons_gets_a_twin(monkeypatch):
    pytest.importorskip("google.adk.models.lite_llm")
    assert _builder._build_plain_twin(_CFG, "planner") is not None
    assert _builder._build_plain_twin(_CFG, "doer") is None
    monkeypatch.setenv("AIFORGE_REASONING_BUDGET_TOKENS", "0")
    assert _builder._build_plain_twin(_CFG, "planner") is None


def test_the_pipeline_model_carries_its_twin():
    pytest.importorskip("google.adk.models.lite_llm")
    assert EscalatingLlm.build("planner", _CFG, []).plain_model is not None
    assert EscalatingLlm.build("doer", _CFG, []).plain_model is None
