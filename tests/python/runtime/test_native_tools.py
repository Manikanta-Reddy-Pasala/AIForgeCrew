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


def test_tools_unsupported_classifier():
    assert _native._tools_unsupported(RuntimeError("this model does not support tools"))
    assert _native._tools_unsupported(RuntimeError("unknown parameter: tools"))
    # a plain timeout / connection drop is NOT a tools-rejection
    assert not _native._tools_unsupported(TimeoutError("read timed out"))
    assert not _native._tools_unsupported(ConnectionError("connection refused"))


def test_probe_transient_failure_stays_optimistic(monkeypatch):
    # A busy/reloading endpoint (timeout) must NOT cache False — native stays on
    # and re-confirms next turn (the bug: one bad probe disabled native for the
    # whole process).
    _native.reset_native_cache()
    monkeypatch.setattr(_native, "_model_for", lambda role: "busy-model")
    import aiforge_core.llm.client as llm
    monkeypatch.setattr(llm, "complete_raw",
                        lambda *a, **k: (_ for _ in ()).throw(TimeoutError("timed out")))
    assert _native._probe_native("chat") is True          # optimistic
    assert "busy-model" not in _native._NATIVE_CACHE       # NOT cached


def test_probe_definitive_rejection_caches_false(monkeypatch):
    _native.reset_native_cache()
    monkeypatch.setattr(_native, "_model_for", lambda role: "no-tools-model")
    import aiforge_core.llm.client as llm
    monkeypatch.setattr(llm, "complete_raw",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("400 this model does not support tools")))
    assert _native._probe_native("chat") is False
    assert _native._NATIVE_CACHE["no-tools-model"] is False  # cached definitive


def test_every_registry_tool_is_native():
    from aiforge_core.runtime.chat_agent._registry import TOOLS
    # EVERY registry tool is exposed natively (rich or permissive), and every
    # native name dispatches through the real registry — no orphans either way.
    assert set(NATIVE_TOOL_NAMES) == set(TOOLS)
    # schemas are well-formed OpenAI function tools
    for s in NATIVE_TOOL_SCHEMAS:
        assert s["type"] == "function"
        fn = s["function"]
        assert fn["name"] and fn["parameters"]["type"] == "object"
        # open object so an extra documented key still passes
        assert fn["parameters"].get("additionalProperties") is True
