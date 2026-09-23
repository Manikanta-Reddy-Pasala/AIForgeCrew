"""In the ADK pipeline, the remote reads of one model reply run at the same time.

ADK gathers a reply's calls, but a plain FunctionTool runs a sync function on
the event loop, so they ran one after another. Remote reads now run in a
worker thread; everything else stays on the loop.
"""
from __future__ import annotations

import asyncio
import contextvars
import threading
import time

import pytest
from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import FunctionTool
from google.genai import types as gt

from aiforge_core.runtime.doer_tools._threaded import ThreadedReadTool, tool_for

REQUEST = contextvars.ContextVar("request", default=None)


@pytest.fixture(autouse=True)
def _four_slots(monkeypatch):
    """Pin the cap: the environment must not change these tests."""
    monkeypatch.setenv("AIFORGE_CHAT_PARALLEL_READS", "4")


def _threaded(row):
    return row["thread"] != "MainThread"


class _BatchModel(BaseLlm):
    """First reply: one jira_read per key, all in one reply. Then: done."""

    keys: list = []
    calls: int = 0

    async def generate_content_async(self, llm_request, stream=False):
        self.calls += 1
        if self.calls == 1:
            parts = [gt.Part(function_call=gt.FunctionCall(
                id=f"c{i}", name="jira_read", args={"key": k}))
                for i, k in enumerate(self.keys)]
        else:
            parts = [gt.Part(text="done")]
        yield LlmResponse(content=gt.Content(role="model", parts=parts))

    @classmethod
    def supported_models(cls):
        return []


def _recording_read(ran, seconds=0.4):
    def jira_read(key: str) -> dict:
        """Read a JIRA issue by key."""
        row = {"key": key, "thread": threading.current_thread().name,
               "start": time.monotonic(), "request": REQUEST.get()}
        ran.append(row)
        time.sleep(seconds)
        row["end"] = time.monotonic()
        return {"key": key, "status": "Open"}
    return jira_read


def _run(tool, keys, on_tool_error=None):
    """Drive a real ADK Runner; returns the function responses in event order."""
    return _drive(LlmAgent(name="reader", model=_BatchModel(model="fake", keys=keys),
                           tools=[tool], on_tool_error_callback=on_tool_error))


def _drive(agent):
    async def _go():
        svc = InMemorySessionService()
        runner = Runner(agent=agent, app_name="t", session_service=svc)
        session = await svc.create_session(app_name="t", user_id="u")
        REQUEST.set("req-42")
        out = []
        async for ev in runner.run_async(
                user_id="u", session_id=session.id,
                new_message=gt.Content(role="user", parts=[gt.Part(text="go")])):
            for p in (ev.content.parts if ev.content else []) or []:
                if p.function_response:
                    out.append(p.function_response.response)
        return out
    return asyncio.run(_go())


def test_remote_reads_of_one_reply_run_at_the_same_time():
    ran = []
    responses = _run(tool_for(_recording_read(ran)), ["A-1", "A-2", "A-3", "A-4"])
    assert [r["key"] for r in responses] == ["A-1", "A-2", "A-3", "A-4"]
    assert all(_threaded(r) for r in ran)
    assert max(r["start"] for r in ran) < min(r["end"] for r in ran), \
        "every read started before the first one ended"


def test_a_plain_function_tool_ran_them_one_by_one():
    """The behaviour this change fixes, pinned so the test above means something.
    If an ADK upgrade starts threading sync tools itself, this fails: then the
    ThreadedReadTool may no longer be needed."""
    ran = []
    _run(FunctionTool(func=_recording_read(ran, 0.2)), ["A-1", "A-2", "A-3"])
    ran.sort(key=lambda r: r["start"])
    assert all(a["end"] <= b["start"] for a, b in zip(ran, ran[1:], strict=False))


def test_the_thread_sees_the_callers_context_vars():
    ran = []
    _run(tool_for(_recording_read(ran, 0.01)), ["A-1", "A-2"])
    assert all(_threaded(r) for r in ran)
    assert [r["request"] for r in ran] == ["req-42", "req-42"]


def test_the_cap_limits_reads_in_flight(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_PARALLEL_READS", "2")
    ran = []
    _run(tool_for(_recording_read(ran, 0.3)), ["A-1", "A-2", "A-3", "A-4"])
    ran.sort(key=lambda r: r["start"])
    in_flight = max(sum(1 for o in ran if o["start"] <= r["start"] < o["end"])
                    for r in ran)
    assert in_flight == 2


def test_two_runs_do_not_share_the_cap(monkeypatch):
    """Parallel subtasks each run their own loop: one run's reads must not
    queue behind another's."""
    monkeypatch.setenv("AIFORGE_CHAT_PARALLEL_READS", "1")
    ran = []
    runs = [threading.Thread(target=_run, args=(tool_for(_recording_read(ran, 1.0)),
                                                 [key]))
            for key in ("A-1", "B-1")]
    for t in runs:
        t.start()
    for t in runs:
        t.join()
    assert max(r["start"] for r in ran) < min(r["end"] for r in ran)


def test_cap_zero_runs_them_on_the_loop(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_PARALLEL_READS", "0")
    ran = []
    _run(tool_for(_recording_read(ran, 0.01)), ["A-1", "A-2"])
    assert not any(_threaded(r) for r in ran)


def test_a_before_tool_callback_still_stops_a_read():
    """The gates (policy, approval, hooks) are before-tool callbacks: a refusal
    must keep the read from ever starting."""
    ran = []

    def refuse(tool, args, tool_context):
        return {"blocked": "policy"}
    agent = LlmAgent(name="reader", model=_BatchModel(model="fake", keys=["A-1"]),
                     tools=[tool_for(_recording_read(ran, 0.01))],
                     before_tool_callback=refuse)
    assert _drive(agent) == [{"blocked": "policy"}]
    assert ran == []


def test_an_error_in_the_thread_reaches_the_tool_error_callbacks():
    """AIForge's tool_error_plugin turns a tool error into a result the model
    reads; it must still see an error raised in the worker thread."""
    seen = []

    def jira_read(key: str) -> dict:
        """Read a JIRA issue by key."""
        seen.append(threading.current_thread().name)
        raise RuntimeError("jira is down")

    def on_error(tool, args, tool_context, error):
        seen.append(str(error))
        return {"error": str(error)}
    agent_tool = tool_for(jira_read)
    responses = _run(agent_tool, ["A-1"], on_tool_error=on_error)
    assert seen[0] != "MainThread" and seen[1] == "jira is down"
    assert responses == [{"error": "jira is down"}]


def test_only_remote_reads_are_threaded():
    def jira_read(key: str) -> dict:
        """Read."""
        return {}

    def file_write(path: str, content: str) -> dict:
        """Write."""
        return {}
    assert type(tool_for(jira_read)) is ThreadedReadTool
    assert type(tool_for(file_write)) is FunctionTool


def test_the_schema_the_model_sees_is_unchanged():
    fn = _recording_read([])
    assert (tool_for(fn)._get_declaration()
            == FunctionTool(func=fn)._get_declaration())


def test_the_pipeline_tool_set_threads_its_remote_reads():
    from aiforge_core.runtime.chat_agent._native import REMOTE_READS
    from aiforge_core.runtime.doer_tools import adk_function_tools
    tools = {t.name: t for t in adk_function_tools()}
    assert REMOTE_READS.issubset(tools)
    for name, tool in tools.items():
        assert isinstance(tool, ThreadedReadTool) == (name in REMOTE_READS), name
