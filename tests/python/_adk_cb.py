"""Call an ADK callback without caring whether it is sync or async.

ADK awaits a callback's result only ``if inspect.isawaitable(result)`` (see
``base_agent.py`` and ``flows/llm_flows/*``), so both shapes are legal and
which one a given callback uses is an implementation detail. Tests that wrote
``asyncio.run(cb(...))`` pinned the async shape by accident: the day a callback
stopped needing ``await``, the test failed with "a coroutine was expected",
which says nothing about the behaviour under test.

Use :func:`run_cb` instead. It runs the coroutine when there is one and returns
the value directly when there is not, so the test asserts on what the callback
DID.
"""
from __future__ import annotations

import asyncio
import inspect
from typing import Any


def run_cb(cb, /, **kwargs) -> Any:
    """Invoke ``cb(**kwargs)``, awaiting the result only if it is awaitable."""
    result = cb(**kwargs)
    if inspect.isawaitable(result):
        return asyncio.run(result)
    return result


__all__ = ["run_cb"]
