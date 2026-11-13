"""One-shot nudge when a test command is killed / hangs with no progress.

A test run that is stopped with little or no new output usually means the
code under test never returns. Say so once, so the agent looks for the loop
instead of re-running the same hung suite.
"""
from __future__ import annotations

import os
import re

from aiforge_core.runtime.failure_signature import result_text

_NOTE = (
    "[harness — not the user] The test command was stopped (killed / timed out) "
    "with little or no new output. That usually means the code under test is "
    "stuck in an infinite loop or waiting on input. Do NOT re-run the same "
    "full suite yet. Find the code your last edits changed that can loop "
    "(a `while` or retry that does not always make progress), fix it, then "
    "re-run "
    "one failing test file with a short timeout."
)

_KILL = re.compile(
    r"killed|stopped|timeout|timed?\s*out|SIGINT|SIGTERM|command_kill",
    re.I,
)


def _on() -> bool:
    return os.environ.get("AIFORGE_CHAT_HANG_TEST_HINT", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _looks_like_test(name, args, result) -> bool:
    try:
        from .._shell import is_test_command
        if is_test_command(name, args or {}):
            return True
    except Exception:  # noqa: BLE001
        pass
    cmd = ""
    if isinstance(args, dict):
        cmd = str(args.get("cmd") or args.get("command") or "")
    blob = (cmd + " " + result_text(result)).lower()
    return "pytest" in blob or "unittest" in blob


def after_tool(st, name, args, result) -> str:
    """Return a one-shot note when a test run looks hung; else ``\"\"``."""
    if not _on() or getattr(st, "_hang_test_hint_sent", False):
        return ""
    if not isinstance(result, dict):
        return ""
    # Only a stopped/killed test (or an explicit kill of a pytest job).
    # Do not fire on command_wait check-ins — those often say "timeout"
    # while the suite is still healthy.
    stopped = bool(result.get("stopped") or result.get("killed"))
    text = result_text(result)
    if name == "command_kill":
        if not _looks_like_test(name, args, result) and "pytest" not in text.lower():
            return ""
    elif not stopped:
        return ""
    elif not _looks_like_test(name, args, result):
        return ""
    if not stopped and not _KILL.search(text):
        return ""
    # Growing / finished green runs are not hangs.
    if result.get("ok") and not stopped:
        return ""
    out = str(result.get("stdout") or "") + str(result.get("new_output") or "")
    if len(out.strip()) > 400 and re.search(r"\d+ passed", out):
        return ""
    st._hang_test_hint_sent = True
    return _NOTE


__all__ = ["after_tool"]
