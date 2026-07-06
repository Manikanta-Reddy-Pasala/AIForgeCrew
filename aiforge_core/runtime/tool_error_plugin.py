"""Make a hallucinated / unregistered tool call NON-fatal for the team pipeline.

A local model on a text-only pipeline agent (feedback / loop_gate / validator /
learner — none of which register tools) sometimes emits a spurious
``function_call`` (observed: ``list_knowledge_bases``). ADK's function flow
looks the name up, doesn't find it, and raises
``ValueError("Tool 'X' not found. Available tools: …")``. With nothing to catch
it, that exception propagated all the way out of ``Runner.run_async`` and killed
the ENTIRE SequentialAgent pipeline mid-flight — the "stops after N agents"
symptom.

ADK offers exactly one graceful exit: if an ``on_tool_error_callback`` returns a
dict, ADK uses it as the tool's response instead of re-raising (see
``google/adk/flows/llm_flows/functions.py`` — the ``except ValueError`` around
``_get_tool`` calls ``_run_on_tool_error_callbacks`` and only ``raise``s when
every callback returns ``None``). This plugin returns a plain error observation
for the not-found case, so the model is simply told the tool doesn't exist and
the agent continues to its real (text) answer. Genuine tool *execution* errors
(the tool exists but threw) are left to propagate — we only rescue the
"phantom tool" lookup failure.
"""
from __future__ import annotations

import logging

from google.adk.plugins.base_plugin import BasePlugin

log = logging.getLogger("aiforge.tool_error_plugin")

# Substrings that mark an unknown/unregistered tool (a lookup failure), as
# opposed to a real error raised from inside a tool that DID run.
_NOT_FOUND_MARKERS = ("not found", "not available", "no such tool", "unknown tool")


class PhantomToolGuardPlugin(BasePlugin):
    """Convert an unknown-tool ValueError into a graceful in-band error so one
    hallucinated call can't abort the whole pipeline."""

    def __init__(self, name: str = "phantom_tool_guard"):
        super().__init__(name=name)

    async def on_tool_error_callback(self, *, tool, tool_args, tool_context, error):  # noqa: ANN001
        msg = str(error or "").lower()
        # Only rescue the lookup failure; let real tool exceptions propagate so
        # they still surface / retry as before.
        if not any(m in msg for m in _NOT_FOUND_MARKERS):
            return None
        name = getattr(tool, "name", None) or "?"
        log.warning("phantom_tool_guard: rescued unknown tool call %r "
                    "(kept the pipeline alive)", name)
        return {
            "error": f"Tool '{name}' does not exist and is not available to you. "
                     "Do NOT call it again. Ignore it and continue — produce "
                     "your response as plain text."
        }


__all__ = ["PhantomToolGuardPlugin"]
