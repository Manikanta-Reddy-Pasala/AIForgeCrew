"""In the ADK pipeline, the slow reads of one model reply run at the same time.

ADK gathers a reply's calls, but a plain FunctionTool runs a sync function on
the event loop, so they ran one after another. Slow reads now run in a
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


def test_slow_reads_of_one_reply_run_at_the_same_time():
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


def test_editor_rename_and_format_take_the_same_lock_as_file_read(
        monkeypatch, tmp_path):
    """A write through editor, rename_symbol, or format waits on the same
    per-path lock file_read holds, so the read cannot cache a torn file."""
    import re

    from aiforge_core.runtime.doer_tools import _fs
    from aiforge_core.runtime.doer_tools._repo import _rename_in_one_file
    from aiforge_core.runtime.sandbox import reset_root_override, set_root_override
    from aiforge_core.runtime.tools.editor import editor
    from aiforge_core.runtime.tools.format import format as format_file
    seen = []
    real = _fs._file_lock

    def _spy(path):
        lock = real(path)
        seen.append((path, lock))
        return lock

    monkeypatch.setattr(_fs, "_file_lock", _spy)
    token = set_root_override(tmp_path)
    try:
        (tmp_path / "app.py").write_text("x = 1\n")
        for command, kwargs in (
            ("create", {"file_text": "y = 1\n"}),
            ("str_replace", {"old_str": "x = 1\n", "new_str": "x = 2\n"}),
            ("insert", {"insert_line": 0, "new_str": "z = 0\n"}),
            ("undo_edit", {}),
        ):
            path = "new.py" if command == "create" else "app.py"
            editor(command, path=path, **kwargs)
        _rename_in_one_file(str(tmp_path / "app.py"), re.compile(r"\bx\b"),
                            "w", False)
        monkeypatch.setattr(
            "aiforge_core.runtime.tools.format.shutil.which",
            lambda name: "/usr/bin/ruff" if name == "ruff" else None)

        class _Done:
            returncode = 0
            stdout = ""
            stderr = ""

        monkeypatch.setattr(
            "aiforge_core.runtime.tools.format.subprocess.run",
            lambda *args, **kwargs: _Done())
        before = len(seen)
        assert format_file("app.py")["ok"] is True
        assert any(path == "app.py" for path, _lock in seen[before:])
        assert seen, "a write took the file lock"
        for path, lock in seen:
            assert lock is real(path)
        locked = {path for path, _lock in seen}
        assert "app.py" in locked
        assert str(tmp_path / "app.py") in locked
        assert "new.py" in locked
    finally:
        reset_root_override(token)


def test_a_later_write_waits_for_the_earlier_read_of_that_path(tmp_path, monkeypatch):
    """Four reads fill the slots. read(note) waits. patch(other) still runs.
    patch(note) waits until that read has seen the pre-patch bytes."""
    monkeypatch.setenv("AIFORGE_DOER_SKIP_SYNTAX", "1")
    from aiforge_core.runtime.doer_tools import _fs
    from aiforge_core.runtime.sandbox import reset_root_override, set_root_override

    token = set_root_override(tmp_path)
    (tmp_path / "note.txt").write_text("old\n")
    (tmp_path / "other.txt").write_text("zzz\n")
    events = []

    def jira_read(key: str) -> dict:
        """Read a JIRA issue by key."""
        time.sleep(0.35)
        return {"key": key}

    def file_read(path: str) -> dict:
        """Read a file."""
        out = _fs.file_read(path)
        events.append(("read", path, out.get("content"), time.monotonic()))
        return out

    def file_patch(path: str, old_text: str, new_text: str) -> dict:
        """Patch a file."""
        events.append(("patch", path, time.monotonic()))
        return _fs.file_patch(path, old_text, new_text)

    reads = tool_for(jira_read)
    reader = tool_for(file_read)
    writer = tool_for(file_patch)

    async def _go():
        await asyncio.wait_for(asyncio.gather(
            reads._invoke_callable(jira_read, {"key": "B-1"}),
            reads._invoke_callable(jira_read, {"key": "B-2"}),
            reads._invoke_callable(jira_read, {"key": "B-3"}),
            reads._invoke_callable(jira_read, {"key": "B-4"}),
            reader._invoke_callable(file_read, {"path": "note.txt"}),
            writer._invoke_callable(
                file_patch, {"path": "other.txt", "old_text": "zzz\n",
                             "new_text": "yyy\n"}),
            writer._invoke_callable(
                file_patch, {"path": "note.txt", "old_text": "old\n",
                             "new_text": "new\n"}),
        ), timeout=3)

    try:
        asyncio.run(_go())
    finally:
        reset_root_override(token)
    read = next(e for e in events if e[0] == "read")
    other = next(e for e in events if e[0] == "patch" and e[1] == "other.txt")
    note = next(e for e in events if e[0] == "patch" and e[1] == "note.txt")
    assert read[2] == "old\n"
    assert other[2] < read[3]
    assert note[2] >= read[3]
    assert (tmp_path / "note.txt").read_text() == "new\n"


def test_reads_are_threaded_and_a_write_stays_on_the_loop():
    from aiforge_core.runtime.doer_tools._threaded import OrderedWriteTool

    def jira_read(key: str) -> dict:
        """Read."""
        return {}

    def file_read(path: str) -> dict:
        """Read a file."""
        return {}

    def grep_repo(pattern: str, path: str = ".") -> dict:
        """Search."""
        return {}

    def file_write(path: str, content: str) -> dict:
        """Write."""
        return {}

    def run_shell(cmd: str) -> dict:
        """Run a command."""
        return {}

    def bash(cmd: str) -> dict:
        """Run a command."""
        return {}
    assert type(tool_for(jira_read)) is ThreadedReadTool
    assert type(tool_for(file_read)) is ThreadedReadTool
    assert type(tool_for(grep_repo)) is FunctionTool
    assert type(tool_for(file_write)) is OrderedWriteTool
    assert type(tool_for(run_shell)) is FunctionTool
    assert type(tool_for(bash)) is FunctionTool


def test_the_schema_the_model_sees_is_unchanged():
    fn = _recording_read([])
    assert (tool_for(fn)._get_declaration()
            == FunctionTool(func=fn)._get_declaration())


def test_the_pipeline_tool_set_threads_its_slow_reads():
    from aiforge_core.runtime.doer_tools import adk_function_tools
    from aiforge_core.runtime.doer_tools._threaded import (
        THREADED_READS, OrderedWriteTool)
    tools = {t.name: t for t in adk_function_tools()}
    assert {"jira_read", "jira_remote_links", "gitlab_pipeline",
            "gitlab_pipelines", "confluence_children", "web_fetch",
            "fetch_url", "http_get"}.issubset(tools)
    for name, tool in tools.items():
        assert isinstance(tool, ThreadedReadTool) == (name in THREADED_READS), name
    assert type(tools["file_read"]) is ThreadedReadTool
    assert type(tools["editor"]) is OrderedWriteTool
    assert type(tools["format"]) is OrderedWriteTool
    assert type(tools["rename_symbol"]) is OrderedWriteTool
    assert type(tools["file_patch"]) is OrderedWriteTool
    assert type(tools["run_shell"]) is FunctionTool


def _named(name):
    def fn(url: str) -> dict:
        """Read."""
        return {}
    fn.__name__ = name
    return fn


def test_the_researcher_reads_pages_in_a_thread(monkeypatch):
    from aiforge_core.runtime.doer_tools import adk_function_tools
    monkeypatch.delenv("AIFORGE_TOOL_ENFORCE", raising=False)
    tools = {t.name: t for t in adk_function_tools(role="researcher")}
    assert isinstance(tools["web_read"], ThreadedReadTool)
    assert isinstance(tools["jira_read"], ThreadedReadTool)


def test_the_pipeline_names_for_a_page_fetch_are_threaded():
    for name in ("web_fetch", "fetch_url", "http_get", "web_read",
                 "codegraph_explore"):
        assert type(tool_for(_named(name))) is ThreadedReadTool, name


@pytest.mark.parametrize("name", ["web_crawl", "email_read",
                                  "gitlab_pipeline_watch", "repo_map"])
def test_reads_with_side_effects_or_long_waits_stay_on_the_loop(name):
    assert type(tool_for(_named(name))) is FunctionTool
