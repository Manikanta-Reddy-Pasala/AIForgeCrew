"""Perf samples for the ADK path — the ticket pipeline and team chat.

Their model calls go through ADK's LiteLlm straight to litellm, and their tools
are ADK FunctionTools, so neither ever reached ``perf_recorder``: the Perf page
showed chat's numbers and none of the Doer's. This plugin only OBSERVES — every
callback returns None, so it never changes what a run does — and records one
"LLM" sample per model call (named by agent) and one "Tool" sample per tool
call, errors included.
"""
from __future__ import annotations

import time

from google.adk.plugins.base_plugin import BasePlugin

from . import perf_recorder


class PerfPlugin(BasePlugin):
    def __init__(self, name: str = "aiforge_perf"):
        super().__init__(name=name)
        self._started: dict = {}

    # ── model calls: one at a time per agent within an invocation ─────────
    @staticmethod
    def _model_key(ctx) -> tuple:
        return ("model", getattr(ctx, "invocation_id", ""),
                getattr(ctx, "agent_name", ""))

    def _stop(self, key, family: str, name: str) -> None:
        t0 = self._started.pop(key, None)
        if t0 is not None:
            perf_recorder.record(family, name, (time.perf_counter() - t0) * 1000.0)

    async def before_model_callback(self, *, callback_context, llm_request):  # noqa: ANN001
        self._started[self._model_key(callback_context)] = time.perf_counter()
        return None

    async def after_model_callback(self, *, callback_context, llm_response):  # noqa: ANN001
        self._stop(self._model_key(callback_context), "LLM",
                   getattr(callback_context, "agent_name", "") or "agent")
        return None

    async def on_model_error_callback(self, *, callback_context, llm_request, error):  # noqa: ANN001
        # A failed call still took the time: recorded like a finished one.
        return await self.after_model_callback(callback_context=callback_context,
                                               llm_response=None)

    # ── tool calls: keyed by the function call id ────────────────────────
    @staticmethod
    def _tool_key(tool_context) -> tuple:
        return ("tool", getattr(tool_context, "function_call_id", None) or id(tool_context))

    async def before_tool_callback(self, *, tool, tool_args, tool_context):  # noqa: ANN001
        self._started[self._tool_key(tool_context)] = time.perf_counter()
        return None

    async def after_tool_callback(self, *, tool, tool_args, tool_context, result):  # noqa: ANN001
        self._stop(self._tool_key(tool_context), "Tool", getattr(tool, "name", "tool"))
        return None

    async def on_tool_error_callback(self, *, tool, tool_args, tool_context, error):  # noqa: ANN001
        return await self.after_tool_callback(tool=tool, tool_args=tool_args,
                                              tool_context=tool_context, result=None)
