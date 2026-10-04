"""``finish`` ends the Doer's turn: the model is not called again, and the
summary is the Doer's outcome."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from google.genai import types as gtypes

from aiforge_core.runtime import doer_finish
from aiforge_core.runtime.chat_pipeline_events import _part_events

_OK = {"ok": True, "terminate": True, "summary": "added f, 3 tests pass",
       "status": "done"}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("AIFORGE_DOER_FINISH_ENDS_TURN", raising=False)


def _ctx(state=None):
    return SimpleNamespace(actions=SimpleNamespace(skip_summarization=False),
                           state={} if state is None else state)


def _call(name, response, args=None, ctx=None):
    ctx = ctx or _ctx()
    out = doer_finish.make_finish_callback()(
        tool=SimpleNamespace(name=name), args=args or {}, tool_context=ctx,
        tool_response=response)
    assert out is None                    # never replaces the tool response
    return ctx


def _outcome(ctx):
    return json.loads(ctx.state["doer_outcome"])


def test_a_successful_finish_ends_the_turn_and_is_the_outcome():
    ctx = _call("finish", _OK)
    assert ctx.actions.skip_summarization is True
    assert _outcome(ctx) == {"turn_log": "added f, 3 tests pass"}


def test_the_outcome_lists_what_the_turn_did_not_what_the_model_says():
    ctx = _ctx()
    _call("file_patch", {"ok": True}, {"path": "calc.py"}, ctx)
    _call("file_write", {"ok": True}, {"path": "./test_calc.py"}, ctx)
    _call("file_patch", {"ok": True}, {"path": "calc.py"}, ctx)        # again
    _call("file_patch", {"ok": False, "error": "no match"}, {"path": "x.py"}, ctx)
    _call("editor", {"ok": True}, {"command": "view", "path": "y.py"}, ctx)
    _call("editor", {"ok": True}, {"command": "create", "path": "z.py"}, ctx)
    _call("bash", {"ok": False, "returncode": 1},
          {"command": "python3 -m pytest -q"}, ctx)
    _call("run_shell", {"ok": True, "returncode": 0},
          {"cmd": "python3 -m pytest -q"}, ctx)
    assert ctx.actions.skip_summarization is False
    _call("finish", _OK, ctx=ctx)
    assert _outcome(ctx) == {
        "file_diffs": [{"path": "calc.py", "action": "patch"},
                       {"path": "test_calc.py", "action": "write"},
                       {"path": "z.py", "action": "write"}],
        "test_status": "green",
        "turn_log": "added f, 3 tests pass"}


def test_a_call_that_did_not_succeed_or_a_run_that_hides_its_exit_code_is_not_counted():
    ctx = _ctx()
    _call("file_patch", {"error": "mandatory input parameters are not present"},
          {"path": "calc.py"}, ctx)
    _call("bash", {"ok": True, "returncode": 0},
          {"command": "python3 -m pytest -q || true"}, ctx)
    _call("bash", {"ok": True, "returncode": 0},
          {"command": "pytest -q | tail -3"}, ctx)
    _call("finish", _OK, ctx=ctx)
    assert _outcome(ctx) == {"turn_log": "added f, 3 tests pass"}


def _after_agent(state):
    ctx = SimpleNamespace(state=state)
    assert doer_finish.make_outcome_callback()(callback_context=ctx) is None
    return state


def test_a_turn_that_ends_in_prose_still_reports_what_it_did():
    """Live: an extra Doer pass ended with a sentence; the outcome lost its
    file_diffs, the Validator asked for changes, the re-plan took minutes."""
    st = _after_agent({"doer_outcome": "Nothing left to do.",
                       "_doer_files": {"calc.py": "patch"},
                       "_doer_tests_ok": True})
    assert json.loads(st["doer_outcome"]) == {
        "file_diffs": [{"path": "calc.py", "action": "patch"}],
        "test_status": "green", "turn_log": "Nothing left to do."}
    again = dict(st)
    assert _after_agent(again) == st                      # stable


def test_the_models_own_contract_keeps_its_words_and_gets_the_measured_files():
    said = ('```json\n{"file_diffs": [], "compile_status": "skipped", '
            '"test_status": "green", "turn_log": "done", "note": "x"}\n```')
    st = _after_agent({"doer_outcome": said, "_doer_files": {"a.py": "write"}})
    assert json.loads(st["doer_outcome"]) == {
        "file_diffs": [{"path": "a.py", "action": "write"}],
        "compile_status": "skipped", "test_status": "green",
        "turn_log": "done", "note": "x"}


def test_nothing_measured_leaves_the_outcome_as_written(monkeypatch):
    assert _after_agent({"doer_outcome": "prose"}) == {"doer_outcome": "prose"}
    monkeypatch.setenv("AIFORGE_DOER_FINISH_ENDS_TURN", "0")
    st = {"doer_outcome": "prose", "_doer_files": {"a.py": "write"}}
    assert _after_agent(dict(st)) == st


def test_a_check_that_could_not_start_is_not_a_red_check():
    from aiforge_core.runtime import quality_gate
    cb = quality_gate.make_quality_signal_callback()
    for error in ("no_language", "missing_tool"):
        ctx = _ctx()
        cb(tool=SimpleNamespace(name="run_tests"), args={}, tool_context=ctx,
           tool_response={"ok": False, "error": error})
        assert "tests_ok" not in ctx.state, error
    ctx = _ctx()
    cb(tool=SimpleNamespace(name="run_tests"), args={}, tool_context=ctx,
       tool_response={"ok": False, "exit_code": 1, "stdout": "1 failed"})
    assert ctx.state["tests_ok"] is False
    ctx = _ctx()
    cb(tool=SimpleNamespace(name="run_tests"), args={}, tool_context=ctx,
       tool_response={"ok": False, "error": "timeout"})
    assert ctx.state["tests_ok"] is False


def test_the_quality_signals_win_over_a_shell_run():
    ctx = _ctx({"tests_ok": False, "typecheck_ok": True})
    _call("bash", {"ok": True}, {"command": "pytest -q"}, ctx)
    _call("finish", _OK, ctx=ctx)
    out = _outcome(ctx)
    assert out["test_status"] == "red" and out["compile_status"] == "green"


def test_the_chat_answer_reads_the_outcome():
    from aiforge_core.runtime.chat_pipeline_turn import readable_outcome
    ctx = _ctx()
    _call("file_patch", {"ok": True}, {"path": "calc.py"}, ctx)
    _call("finish", _OK, ctx=ctx)
    text = readable_outcome(ctx.state["doer_outcome"])
    assert text.startswith("added f, 3 tests pass") and "`calc.py`" in text


def test_a_blocked_finish_says_so_in_the_outcome():
    ctx = _call("finish", {**_OK, "status": "blocked", "summary": "no JDK"})
    assert ctx.actions.skip_summarization is True
    assert _outcome(ctx) == {"turn_log": "no JDK", "blocker": "no JDK"}


@pytest.mark.parametrize("name,response", [
    ("run_tests", _OK),                                   # another tool
    ("finish", {"ok": False, "error": "repeated_call"}),  # refused by a guard
    ("finish", {"ok": False, "error": "invalid_status"}),
    ("finish", {**_OK, "summary": ""}),
    ("finish", "done"),
])
def test_anything_else_leaves_the_turn_running(name, response):
    ctx = _call(name, response)
    assert ctx.actions.skip_summarization is False
    assert "doer_outcome" not in ctx.state


def test_it_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("AIFORGE_DOER_FINISH_ENDS_TURN", "0")
    ctx = _call("finish", _OK)
    assert ctx.actions.skip_summarization is False and ctx.state == {}


def test_it_runs_before_the_other_after_tool_callbacks():
    from aiforge_core.runtime import pipeline
    after = [f for attr, f in pipeline._DOER_TOOL_CALLBACKS
             if attr == "after_tool_callback"]
    assert after[0] is pipeline._finish_cb
    assert ("after_agent_callback", pipeline._outcome_cb) in pipeline._DOER_TOOL_CALLBACKS


def test_the_finish_summary_is_the_agents_closing_statement():
    part = gtypes.Part(function_response=gtypes.FunctionResponse(
        id="c", name="finish", response=_OK))
    evs = _part_events("doer", part)
    assert evs[-1] == {"type": "thought", "role": "doer",
                       "text": "added f, 3 tests pass"}
    assert evs[0]["text"].startswith("finish → ")
    other = gtypes.Part(function_response=gtypes.FunctionResponse(
        id="c", name="run_tests", response={"ok": True}))
    assert len(_part_events("doer", other)) == 1


def test_adk_does_not_call_the_model_again_after_finish():
    """End to end on the installed ADK: an agent whose model would answer
    every tool result with another ``finish`` stops after the first."""
    from google.adk.agents import LlmAgent
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.adk.tools import FunctionTool

    from aiforge_core.runtime.tools.cognition import finish

    class _Loops(BaseLlm):
        calls: int = 0

        async def generate_content_async(self, llm_request, stream=False):
            self.calls += 1
            if self.calls > 5:                      # the old behaviour's backstop
                yield LlmResponse(content=gtypes.Content(
                    role="model", parts=[gtypes.Part.from_text(text="gave up")]))
                return
            yield LlmResponse(content=gtypes.Content(role="model", parts=[
                gtypes.Part(function_call=gtypes.FunctionCall(
                    id=f"c{self.calls}", name="finish",
                    args={"summary": "added f, 3 tests pass"}))]))

        @classmethod
        def supported_models(cls):
            return []

    model = _Loops(model="stub")
    agent = LlmAgent(name="doer", model=model, instruction="do it",
                     output_key="doer_outcome", tools=[FunctionTool(func=finish)],
                     after_tool_callback=[doer_finish.make_finish_callback()])

    async def _go():
        svc = InMemorySessionService()
        runner = Runner(agent=agent, app_name="t", session_service=svc,
                        auto_create_session=True)
        s = await svc.create_session(app_name="t", user_id="u")
        msg = gtypes.Content(role="user", parts=[gtypes.Part.from_text(text="go")])
        async for _ in runner.run_async(user_id="u", session_id=s.id,
                                        new_message=msg):
            pass
        return dict((await svc.get_session(app_name="t", user_id="u",
                                           session_id=s.id)).state or {})

    state = asyncio.run(_go())
    assert model.calls == 1
    assert json.loads(state["doer_outcome"]) == {"turn_log": "added f, 3 tests pass"}
