"""Bug5 — native OpenAI tool-calling adapter: a native reply becomes the exact
text step the loop already parses (with REAL args, no ARGS_JSON:{})."""
from __future__ import annotations

import json

from aiforge_core.runtime.chat_agent import _native
from aiforge_core.runtime.chat_agent._prompt import _parse
from aiforge_core.runtime.chat_agent._tools._schemas import (
    NATIVE_TOOL_NAMES,
    NATIVE_TOOL_SCHEMAS,
)


def test_tool_call_becomes_action_with_real_args():
    msg = {"role": "assistant", "content": None, "tool_calls": [
        {"type": "function", "function": {
            "name": "file_write",
            "arguments": json.dumps({"path": "calc.py", "content": "x=1"})}}]}
    step = _native._synth_step(msg)
    parsed = _parse(step)
    assert parsed["kind"] == "action"
    assert parsed["tool"] == "file_write"
    assert parsed["args"] == {"path": "calc.py", "content": "x=1"}  # NOT {}


def test_args_already_dict_are_handled():
    msg = {"tool_calls": [{"function": {
        "name": "grep", "arguments": {"pattern": "TODO"}}}]}
    parsed = _parse(_native._synth_step(msg))
    assert parsed["tool"] == "grep" and parsed["args"] == {"pattern": "TODO"}


def test_no_tool_call_returns_content_verbatim():
    msg = {"role": "assistant", "content": "Here is the answer."}
    assert _native._synth_step(msg) == "Here is the answer."
    # …and the loop parses it as a normal final.
    assert _parse(_native._synth_step(msg))["kind"] == "final"


def test_malformed_args_do_not_crash():
    msg = {"tool_calls": [{"function": {"name": "file_read",
                                        "arguments": "{not json"}}]}
    parsed = _parse(_native._synth_step(msg))
    assert parsed["tool"] == "file_read" and parsed["args"] == {}


def test_protocol_env_overrides(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_TOOL_PROTOCOL", "text")
    assert _native.native_tools_enabled("chat") is False
    monkeypatch.setenv("AIFORGE_CHAT_TOOL_PROTOCOL", "native")
    assert _native.native_tools_enabled("chat") is True


def test_core_schemas_match_registry():
    from aiforge_core.runtime.chat_agent._registry import TOOLS
    # every native schema name must dispatch through the real registry
    for name in NATIVE_TOOL_NAMES:
        assert name in TOOLS, f"native schema {name} missing from TOOLS registry"
    # schemas are well-formed OpenAI function tools
    for s in NATIVE_TOOL_SCHEMAS:
        assert s["type"] == "function"
        assert s["function"]["name"] and "parameters" in s["function"]
