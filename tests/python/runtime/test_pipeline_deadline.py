"""Overall pipeline wall-clock deadline.

The per-call LLM timeout and max_llm_calls each bound one dimension; neither
stops a run that stalls without tripping them. ``_pipeline_deadline_s`` + an
``asyncio.timeout`` wrapper around ``runner.run_async`` guarantee the run
terminates (recovering partial state) instead of waiting forever. These pin the
knob and prove the cancel-on-deadline mechanism the runner relies on.
"""
from __future__ import annotations

import asyncio

import pytest

from aiforge_core.runtime import adk_runner as r


def test_deadline_default(monkeypatch):
    monkeypatch.delenv("AIFORGE_PIPELINE_DEADLINE_S", raising=False)
    assert r._pipeline_deadline_s() == 5400.0


def test_deadline_env_override(monkeypatch):
    monkeypatch.setenv("AIFORGE_PIPELINE_DEADLINE_S", "42")
    assert r._pipeline_deadline_s() == 42.0


def test_deadline_bad_value_falls_back(monkeypatch):
    monkeypatch.setenv("AIFORGE_PIPELINE_DEADLINE_S", "not-a-number")
    assert r._pipeline_deadline_s() == 5400.0


def test_deadline_zero_disables(monkeypatch):
    # 0 / negative → disabled sentinel (runner uses nullcontext, no timeout).
    monkeypatch.setenv("AIFORGE_PIPELINE_DEADLINE_S", "0")
    assert r._pipeline_deadline_s() == 0.0


def test_asyncio_timeout_cancels_a_hung_run():
    """The exact backstop the runner uses: a never-resolving async-for is
    cancelled by asyncio.timeout and surfaces as TimeoutError — which the
    runner catches to return partial state instead of hanging.

    WHY a sync test driving asyncio.run: pytest-asyncio is NOT a dependency of
    this repo, so the former ``@pytest.mark.asyncio`` coroutine was silently
    collected and then errored ("async def functions are not natively
    supported"). Driving the coroutine ourselves keeps the assertion real
    without adding a plugin.
    """
    async def _never_ends():
        while True:
            await asyncio.sleep(3600)
            yield 1

    async def _body():
        async with asyncio.timeout(0.05):
            async for _ in _never_ends():
                pass

    coro = _body()
    with pytest.raises(TimeoutError):
        asyncio.run(coro)
