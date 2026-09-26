"""Slow reads that run in a worker thread in the ADK pipeline.

ADK runs the calls of one model reply with ``asyncio.gather``, but a plain
``FunctionTool`` calls a sync function straight on the event loop, so the
calls still ran one after another. A read (a file, a grep, Jira, Confluence,
GitLab, a web page, a codegraph subprocess) spends its time waiting; run in
a thread, the reads of one reply overlap. Writes stay on the loop. Git
lookups stay there too: ``git status`` takes the index lock.

The per-path file lock stops a read and a write from tearing one file. It
does not keep reply order. With every read slot full, a later ``file_patch``
of ``a.py`` would run while ``file_read("a.py")`` is still waiting for a
slot, and the read would return the patched text. A call waits until every
earlier call in this reply that touches the same path has finished.
Different paths still overlap. Shell commands stay unlocked: a shell edit
has no single path to wait on.

The before-tool callbacks (policy, approval, hooks) still run on the loop
before the thread starts.

ADK's own ``RunConfig.tool_thread_pool_config`` is not used: it applies to live
mode only, threads writes as well, and skips the mandatory-argument check.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import weakref

from google.adk.tools import FunctionTool

from aiforge_core.runtime.chat_agent._native import CONCURRENT_READS
from aiforge_core.runtime.chat_agent._turn._batch import _parallel_cap

#: One semaphore per event loop. Each pipeline run has its own loop, so the cap
#: (AIFORGE_CHAT_PARALLEL_READS, as in chat) holds per run: parallel subtasks
#: never wait on each other's reads.
_LOOP_SLOTS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
#: Reply order for one loop. Cleared when nothing from the reply is in flight.
_LOOP_ORDER: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()

#: The pipeline's own names for web_fetch: the same egress-gated GET.
_FETCH_ALIASES = frozenset({"fetch_url", "http_get", "web_read"})
#: ``read`` is file_read under another name. It has to take a slot too, or a
#: patch of the same path runs while this read is still queued on the loop.
_FILE_READ_ALIASES = frozenset({"read"})
#: Directory searches and git. Git takes the index lock; a directory grep is
#: not a read of one file's bytes. File-body reads are threaded, above.
_ON_THE_LOOP = frozenset({
    "list_dir", "find", "grep",
    "git_status", "git_diff", "git_log", "git_blame",
    "grep_repo", "ls", "glob", "search",
})
THREADED_READS = (CONCURRENT_READS | _FETCH_ALIASES | _FILE_READ_ALIASES) - _ON_THE_LOOP

#: Writes that name their files. They stay on the loop and wait for earlier
#: reads of those files. Shell and bash are absent on purpose.
_ORDERED_WRITES = frozenset({
    "file_write", "file_patch", "write", "patch", "edit", "str_replace",
    "editor", "format", "rename_symbol", "multi_edit",
})
_FILE_PATH_TOOLS = THREADED_READS | _ORDERED_WRITES | frozenset({
    "file_read", "read_files", "read_lines", "read",
})


def _slots(loop, cap: int) -> asyncio.Semaphore:
    sem = _LOOP_SLOTS.get(loop)
    if sem is None:
        sem = _LOOP_SLOTS[loop] = asyncio.Semaphore(cap)
    return sem


def _is_async(target) -> bool:
    """ADK's own test: a coroutine function, or an object whose __call__ is one."""
    return (inspect.iscoroutinefunction(target)
            or inspect.iscoroutinefunction(type(target).__call__))


class _Ticket:
    __slots__ = ("seq", "keys", "prefix", "done")

    def __init__(self, seq: int, keys: frozenset[str], prefix: str | None):
        self.seq = seq
        self.keys = keys
        self.prefix = prefix
        self.done = asyncio.Event()


class _ReplyOrder:
    """Same-path order for the calls of one reply on one event loop."""

    def __init__(self):
        self.in_flight = 0
        self.seq = 0
        self.tickets: list[_Ticket] = []

    def begin(self, keys: frozenset[str], prefix: str | None) -> _Ticket:
        if self.in_flight == 0:
            self.seq = 0
            self.tickets = []
        ticket = _Ticket(self.seq, keys, prefix)
        self.seq += 1
        self.in_flight += 1
        self.tickets.append(ticket)
        return ticket

    def earlier(self, ticket: _Ticket) -> list[asyncio.Event]:
        pending = []
        for prev in self.tickets:
            if prev.seq >= ticket.seq or prev.done.is_set():
                continue
            if _shares_path(prev, ticket):
                pending.append(prev.done)
        return pending

    def end(self, ticket: _Ticket) -> None:
        ticket.done.set()
        self.in_flight -= 1


def _order() -> _ReplyOrder:
    loop = asyncio.get_running_loop()
    gate = _LOOP_ORDER.get(loop)
    if gate is None:
        gate = _LOOP_ORDER[loop] = _ReplyOrder()
    return gate


def _under(prefix: str | None, key: str) -> bool:
    if not prefix:
        return False
    return key == prefix or key.startswith(prefix + os.sep)


def _shares_path(a: _Ticket, b: _Ticket) -> bool:
    if a.keys & b.keys:
        return True
    if any(_under(a.prefix, key) for key in b.keys):
        return True
    if any(_under(b.prefix, key) for key in a.keys):
        return True
    if a.prefix and b.prefix and _under(a.prefix, b.prefix):
        return True
    if a.prefix and b.prefix and _under(b.prefix, a.prefix):
        return True
    return False


def _paths_for(name: str, args) -> tuple[frozenset[str], str | None]:
    """File keys this call reads or writes, plus a directory prefix for a
    rename of a whole tree. Empty when the call has no single file path."""
    if name not in _FILE_PATH_TOOLS or not isinstance(args, dict):
        return frozenset(), None
    from aiforge_core.runtime.doer_tools._fs import _file_key
    if name == "multi_edit":
        keys = []
        for edit in args.get("edits") or []:
            if isinstance(edit, dict) and edit.get("path"):
                keys.append(_file_key(str(edit["path"])))
        return frozenset(keys), None
    raw = args.get("path")
    if raw is None or raw == "":
        if name == "rename_symbol":
            raw = "."
        else:
            return frozenset(), None
    raw = str(raw)
    if name == "rename_symbol":
        try:
            from aiforge_core.runtime.sandbox import resolve_inside_root
            if resolve_inside_root(raw).is_dir():
                return frozenset(), _file_key(raw)
        except Exception:  # noqa: BLE001
            pass
    return frozenset({_file_key(raw)}), None


def _begin(name: str, args) -> _Ticket:
    keys, prefix = _paths_for(name, args)
    return _order().begin(keys, prefix)


async def _wait_turn(ticket: _Ticket) -> None:
    """Wait for earlier same-path calls. Returns without yielding when
    there is nothing to wait for, so a write still runs straight on the loop."""
    pending = _order().earlier(ticket)
    if pending:
        await asyncio.gather(*(event.wait() for event in pending))


def _end(ticket: _Ticket) -> None:
    _order().end(ticket)


class ThreadedReadTool(FunctionTool):
    """A ``FunctionTool`` whose sync function runs in a worker thread.
    ``asyncio.to_thread`` hands the thread a copy of the caller's context vars,
    so the sandbox root, request context and Stop still apply. A read already
    running when the run is stopped finishes in its thread; its result is
    dropped. The read waits for an earlier write of the same path before it
    takes a slot, so it does not occupy a slot while that write runs."""

    async def _invoke_callable(self, target, args_to_call):
        ticket = _begin(self.name, args_to_call)
        try:
            await _wait_turn(ticket)
            cap = _parallel_cap()
            if _is_async(target) or cap == 0:
                return await super()._invoke_callable(target, args_to_call)
            async with _slots(asyncio.get_running_loop(), cap):
                return await asyncio.to_thread(target, **args_to_call)
        finally:
            _end(ticket)


class OrderedWriteTool(FunctionTool):
    """A write that stays on the event loop, after earlier reads of its path.

    The file lock inside the write stops tearing. This wait stops the write
    from running while an earlier read of that path is still queued on a
    full slot pool. A write of a different path does not wait."""

    async def _invoke_callable(self, target, args_to_call):
        ticket = _begin(self.name, args_to_call)
        try:
            await _wait_turn(ticket)
            return await super()._invoke_callable(target, args_to_call)
        finally:
            _end(ticket)


def tool_for(fn) -> FunctionTool:
    """The ADK tool for ``fn``: threaded for a slow read, ordered for a
    file write, plain for everything else (including the shell)."""
    from ._net_wrap import bracket
    fn = bracket(fn)         # shell-capable tools: the team_repo_net net
    name = fn.__name__
    if name in THREADED_READS:
        cls = ThreadedReadTool
    elif name in _ORDERED_WRITES:
        cls = OrderedWriteTool
    else:
        cls = FunctionTool
    return cls(func=fn)
