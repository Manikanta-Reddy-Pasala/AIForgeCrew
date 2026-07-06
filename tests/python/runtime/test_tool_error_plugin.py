"""PhantomToolGuardPlugin — turns a hallucinated tool call into a graceful
observation so one bad function_call can't abort the whole team pipeline."""
from __future__ import annotations

import asyncio

import pytest

from aiforge_core.runtime.tool_error_plugin import PhantomToolGuardPlugin


class _Tool:
    def __init__(self, name):
        self.name = name


def _run(coro):
    return asyncio.run(coro)


def _call(error):
    p = PhantomToolGuardPlugin()
    return _run(p.on_tool_error_callback(
        tool=_Tool("list_knowledge_bases"), tool_args={}, tool_context=None,
        error=error))


def test_rescues_tool_not_found():
    # The exact ADK message shape.
    err = ValueError("Tool 'list_knowledge_bases' not found.\nAvailable tools: ")
    out = _call(err)
    assert isinstance(out, dict)
    assert "does not exist" in out["error"]
    assert "list_knowledge_bases" in out["error"]


def test_rescues_not_available_variant():
    out = _call(ValueError("tool is not available"))
    assert isinstance(out, dict)


def test_real_tool_error_propagates():
    # A genuine error from a tool that DID run must NOT be swallowed.
    out = _call(RuntimeError("KeyError: 'path' while writing file"))
    assert out is None


def test_empty_error_propagates():
    out = _call(None)
    assert out is None


def test_plugin_name():
    assert PhantomToolGuardPlugin().name == "phantom_tool_guard"
