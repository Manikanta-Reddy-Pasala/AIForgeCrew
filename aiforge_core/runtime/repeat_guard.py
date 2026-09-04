"""Stuck-loop guard for ADK agents.

Weak models sometimes emit the SAME tool call (often a truncated /
malformed one, e.g. ``run_command {"command": "python3 <"}``) over and
over, burning the whole LLM-call budget until the run dies with
``LlmCallsLimitExceededError``. This ``before_tool_callback`` counts
identical (tool, args) calls per run and, after a threshold, short-
circuits with a firm "you're repeating yourself — stop and change
approach or finish" message so the agent breaks out instead of looping.

Tunable: ``AIFORGE_TOOL_REPEAT_LIMIT`` (default 4); ``0`` disables.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os

log = logging.getLogger("aiforge.repeat_guard")


def make_repeat_guard_callback():
    try:
        limit = int(os.environ.get("AIFORGE_TOOL_REPEAT_LIMIT", "4"))
    except ValueError:
        limit = 4
    if limit <= 0:
        return None

    async def _cb(*, tool, args, tool_context, **_kw):
        try:
            name = getattr(tool, "name", "") or ""
            sig = name + "|" + json.dumps(args or {}, sort_keys=True,
                                          default=str)[:600]
            key = hashlib.sha1(sig.encode(), usedforsecurity=False).hexdigest()[:12]
            state = getattr(tool_context, "state", None)
            if state is None:
                return None
            counts = dict(state.get("_repeat_counts") or {})
            counts[key] = counts.get(key, 0) + 1
            state["_repeat_counts"] = counts
            if counts[key] >= limit:
                log.warning("repeat_guard.block tool=%s n=%d", name, counts[key])
                return {
                    "ok": False,
                    "error": "repeated_call",
                    "hint": (
                        f"You have called `{name}` with identical arguments "
                        f"{counts[key]} times — it is NOT making progress "
                        "(the call may be malformed/truncated). STOP "
                        "repeating it: fix the arguments, try a different "
                        "approach, or finish with what you have."
                    ),
                }
        except Exception as exc:  # noqa: BLE001
            log.debug("repeat_guard internal error (allow): %s", exc)
        return None

    return _cb
