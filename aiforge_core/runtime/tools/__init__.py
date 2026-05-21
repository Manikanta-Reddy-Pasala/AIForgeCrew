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
    from .cognition import finish, think
    from .editor import editor

    canonical = [editor, bash, think, finish]
    return [FunctionTool(func=fn) for fn in canonical]
