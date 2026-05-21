"""OpenHands-parity tool surface for the Doer agent.

Sub-modules:

* :mod:`editor`      — multi-command file editor (view/create/str_replace/insert/undo_edit)
* :mod:`bash`        — tmux-backed persistent shell session
* :mod:`cognition`   — think + finish
* :mod:`_trace`      — shared Neo4j event emitter

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
    from .ipython_kernel import execute_ipython_cell
    from .mcp_client import mcp

    canonical = [editor, bash, browse, execute_ipython_cell,
                 delegate_to_agent, mcp, think, finish]
    return [FunctionTool(func=fn) for fn in canonical]
