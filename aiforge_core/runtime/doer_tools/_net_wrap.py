"""The team_repo_net safety net for the Doer's shell-capable tools.

The chat loop brackets its shell calls (chat_agent/_loop); the sequential ADK
team and the graph-pipeline Doer call their tools through ADK FunctionTools
instead — ``run_shell`` (and its aliases, including the check-in hand-off),
the tmux ``bash`` tool, and ``command_wait`` / ``command_output`` /
``command_kill``. :func:`bracket` wraps each of those so that, in a team run
(team_workspace.for_cwd of the tool root), whatever the command changed in
the user's real checkout is put back and the result says so.
"""
from __future__ import annotations

import functools
import logging

log = logging.getLogger(__name__)

#: Doer tool names that run (or check on) a shell command.
SHELL_CAPABLE = frozenset({"run_shell", "shell", "bash", "run", "command_wait",
                           "command_output", "command_kill"})


def _cwd() -> str:
    try:
        from ..sandbox import root
        return str(root())
    except Exception:  # noqa: BLE001
        return ""


def bracket(fn):
    """``fn`` wrapped with the net when it is shell-capable, else ``fn``.
    The wrapper keeps the name, docstring and signature (ADK builds the tool
    schema from them)."""
    if getattr(fn, "__name__", "") not in SHELL_CAPABLE \
            or getattr(fn, "_team_net", False):
        return fn

    @functools.wraps(fn)
    def _netted(*args, **kwargs):
        from aiforge_core.runtime import team_repo_net as net
        cwd = _cwd()
        handle = net.begin(cwd, "run_shell") if cwd else None
        result = fn(*args, **kwargs)
        if handle is None:
            note = net.poll(cwd) if cwd else ""
        else:
            result, note = net.end(handle, result)
        if note and isinstance(result, dict):
            result = dict(result, note=note)
        return result

    _netted._team_net = True
    return _netted


__all__ = ["SHELL_CAPABLE", "bracket"]
