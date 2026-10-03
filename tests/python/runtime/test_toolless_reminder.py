"""A tool-less stage (triage, planner, the judges) reads its task again after
the request, so a request that ends "then implement it" is classified or
judged instead of started."""
from types import SimpleNamespace

import pytest
from google.genai import types as gtypes

from aiforge_core.agents import _base


def _user(text):
    return gtypes.Content(role="user", parts=[gtypes.Part.from_text(text=text)])


def _texts(content):
    return [p.text for p in content.parts]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("AIFORGE_TOOLLESS_REMINDER", raising=False)


def test_the_reminder_is_the_last_thing_in_the_request():
    seed = _user("CURRENT REQUEST: add f. Then implement it.")
    req = SimpleNamespace(contents=[seed])
    assert _base.toolless_reminder_callback()(None, req) is None
    assert len(req.contents) == 1
    assert _texts(req.contents[0]) == [
        "CURRENT REQUEST: add f. Then implement it.", _base.TOOLLESS_REMINDER]
    assert _texts(seed) == ["CURRENT REQUEST: add f. Then implement it."]  # untouched


def test_after_a_model_turn_or_a_tool_result_it_is_its_own_message():
    model = gtypes.Content(role="model", parts=[gtypes.Part.from_text(text="x")])
    req = SimpleNamespace(contents=[_user("seed"), model])
    _base.toolless_reminder_callback()(None, req)
    assert [c.role for c in req.contents] == ["user", "model", "user"]
    assert _texts(req.contents[-1]) == [_base.TOOLLESS_REMINDER]

    result = gtypes.Content(role="user", parts=[gtypes.Part(
        function_response=gtypes.FunctionResponse(id="c", name="t",
                                                  response={"ok": True}))])
    req = SimpleNamespace(contents=[_user("seed"), result])
    _base.toolless_reminder_callback()(None, req)
    assert len(req.contents) == 3 and req.contents[1] is result


def test_an_empty_request_gets_just_the_reminder():
    req = SimpleNamespace(contents=[])
    _base.toolless_reminder_callback()(None, req)
    assert _texts(req.contents[0]) == [_base.TOOLLESS_REMINDER]


def test_it_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("AIFORGE_TOOLLESS_REMINDER", "0")
    seed = _user("seed")
    req = SimpleNamespace(contents=[seed])
    _base.toolless_reminder_callback()(None, req)
    assert req.contents == [seed]


def test_tool_less_agents_carry_it_and_tool_users_do_not():
    pytest.importorskip("google.adk.agents")
    from aiforge_core.agents import planner, researcher, triage
    from aiforge_core.runtime.pipeline import build_litellm_model

    def names(agent):
        cb = agent.before_model_callback
        cbs = cb if isinstance(cb, list) else [cb] if cb else []
        return [getattr(c, "__qualname__", "") for c in cbs]

    for mod in (triage, planner):
        assert any("toolless_reminder_callback" in n
                   for n in names(mod.build(build_litellm_model))), mod.ROLE
    assert not any("toolless_reminder_callback" in n
                   for n in names(researcher.build(build_litellm_model)))
