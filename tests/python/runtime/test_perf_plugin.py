"""The ADK path (ticket pipeline, team chat) records perf samples too.

Its model calls go through ADK's LiteLlm and its tools are ADK FunctionTools,
so neither reached perf_recorder: the Perf page showed chat's numbers only.
"""
import asyncio
import types

import pytest

pytest.importorskip("google.adk.plugins.base_plugin")

from aiforge_core.runtime import perf_recorder  # noqa: E402
from aiforge_core.runtime.perf_plugin import PerfPlugin  # noqa: E402


@pytest.fixture(autouse=True)
def _cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))


def test_model_and_tool_calls_are_recorded_and_nothing_is_changed():
    p = PerfPlugin()
    cb = types.SimpleNamespace(invocation_id="i1", agent_name="doer")
    tool = types.SimpleNamespace(name="file_read")
    tctx = types.SimpleNamespace(function_call_id="fc1")

    async def run():
        outs = [await p.before_model_callback(callback_context=cb, llm_request=None),
                await p.after_model_callback(callback_context=cb, llm_response=None),
                await p.before_tool_callback(tool=tool, tool_args={}, tool_context=tctx),
                await p.on_tool_error_callback(tool=tool, tool_args={},
                                               tool_context=tctx, error=RuntimeError())]
        return outs
    assert asyncio.run(run()) == [None, None, None, None]    # observe only
    got = {(r["event"], r["name"]) for r in perf_recorder.aggregate()}
    assert got == {("LLM", "doer"), ("Tool", "file_read")}


def test_both_drivers_attach_it():
    from aiforge_core.runtime import chat_pipeline
    from aiforge_core.runtime.adk_runner import _pipeline
    assert "PerfPlugin" in [type(x).__name__ for x in _pipeline._phantom_tool_guard()]
    assert "PerfPlugin" in [type(x).__name__ for x in chat_pipeline._team_plugins()]
