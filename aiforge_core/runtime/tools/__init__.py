"""OpenHands-parity tool surface for the Doer agent.

Sub-modules:

* :mod:`editor`      — multi-command file editor (view/create/str_replace/insert/undo_edit)
* :mod:`bash`        — tmux-backed persistent shell session
* :mod:`cognition`   — think + finish
* :mod:`_trace`      — shared trace-event emitter

Sibling tool modules NEVER import each other — keeps responsibilities clean and
unit tests cheap. The ADK :class:`FunctionTool` factory lives below.
"""
from __future__ import annotations

__all__ = ["adk_function_tools"]


def adk_function_tools() -> list:
    """Return canonical Doer tools as ADK ``FunctionTool`` instances.

    Lazy import keeps unit tests ADK-free.
    """
    from google.adk.tools import FunctionTool

    from .bash import bash
    from .browser import browse
    from .cognition import finish, think
    from .delegation import delegate_to_agent
    from .editor import editor
    from .format import format
    from .ipython_kernel import execute_ipython_cell
    from .lsp import lsp
    from .mcp_client import mcp
    from .memory_write import memory_write
    from .test_runner import run_tests
    from .typecheck import typecheck

    canonical = [editor, bash, browse, execute_ipython_cell,
                 delegate_to_agent, mcp, memory_write,
                 format, typecheck, run_tests, lsp,
                 think, finish]
    return [FunctionTool(func=fn) for fn in canonical]
