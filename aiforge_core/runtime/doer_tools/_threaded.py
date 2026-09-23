"""Remote reads that run in a worker thread in the ADK pipeline.

ADK runs the calls of one model reply with ``asyncio.gather``, but a plain
``FunctionTool`` calls a sync function straight on the event loop, so the
calls still ran one after another. A remote read (Jira, Confluence, GitLab)
spends its time waiting on a server; run in a thread, the reads of one reply
overlap. Everything else still runs on the loop. A read and a write asked for
in the same reply may now overlap too, as ADK's gather always allowed: a model
puts only independent calls in one reply. The before-tool callbacks (policy,
approval, hooks) still run on the loop before the thread starts.

ADK's own ``RunConfig.tool_thread_pool_config`` is not used: it applies to live
mode only, threads writes as well, and skips the mandatory-argument check.
"""
from __future__ import annotations

import asyncio
import inspect
import weakref

from google.adk.tools import FunctionTool

from aiforge_core.runtime.chat_agent._native import REMOTE_READS
from aiforge_core.runtime.chat_agent._turn._batch import _parallel_cap

#: One semaphore per event loop. Each pipeline run has its own loop, so the cap
#: (AIFORGE_CHAT_PARALLEL_READS, as in chat) holds per run: parallel subtasks
#: never wait on each other's reads.
_LOOP_SLOTS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _slots(loop, cap: int) -> asyncio.Semaphore:
    sem = _LOOP_SLOTS.get(loop)
    if sem is None:
        sem = _LOOP_SLOTS[loop] = asyncio.Semaphore(cap)
    return sem


def _is_async(target) -> bool:
    """ADK's own test: a coroutine function, or an object whose __call__ is one."""
    return (inspect.iscoroutinefunction(target)
            or inspect.iscoroutinefunction(type(target).__call__))


class ThreadedReadTool(FunctionTool):
    """A ``FunctionTool`` whose sync function runs in a worker thread.
    ``asyncio.to_thread`` hands the thread a copy of the caller's context vars,
    so the sandbox root, request context and Stop still apply. A read already
    running when the run is stopped finishes in its thread; its result is
    dropped."""

    async def _invoke_callable(self, target, args_to_call):
        cap = _parallel_cap()
        if _is_async(target) or cap == 0:
            return await super()._invoke_callable(target, args_to_call)
        async with _slots(asyncio.get_running_loop(), cap):
            return await asyncio.to_thread(target, **args_to_call)


def tool_for(fn) -> FunctionTool:
    """The ADK tool for ``fn``: threaded for a remote read, plain otherwise."""
    cls = ThreadedReadTool if fn.__name__ in REMOTE_READS else FunctionTool
    return cls(func=fn)
