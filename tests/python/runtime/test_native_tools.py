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


def test_malformed_args_signal_text_fallback():
    # a truncated/malformed arguments JSON must NOT degrade to ARGS_JSON:{}
    # (the exact empty-args failure this feature kills) — it returns the sentinel
    # so make_native_complete_fn redoes the turn on the hardened text path.
    msg = {"tool_calls": [{"function": {"name": "file_read",
                                        "arguments": "{not json"}}]}
    assert _native._synth_step(msg) == _native._NATIVE_ARGS_UNRECOVERABLE


def test_legit_empty_args_still_action():
    # a genuinely no-arg call ("{}" / "") is NOT malformed → real ACTION, {} args
    for empty in ("{}", "", None):
        msg = {"tool_calls": [{"function": {"name": "list_dir",
                                            "arguments": empty}}]}
        parsed = _parse(_native._synth_step(msg))
        assert parsed["tool"] == "list_dir" and parsed["args"] == {}


def test_protocol_env_overrides(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_TOOL_PROTOCOL", "text")
    assert _native.native_tools_enabled("chat") is False
    monkeypatch.setenv("AIFORGE_CHAT_TOOL_PROTOCOL", "native")
    assert _native.native_tools_enabled("chat") is True


def test_tools_unsupported_classifier():
    assert _native._tools_unsupported(RuntimeError("this model does not support tools"))
    assert _native._tools_unsupported(RuntimeError("tools are unsupported by this model"))
    assert _native._tools_unsupported(RuntimeError("no such tool 'x'"))
    # a plain timeout / connection drop is NOT a tools-rejection
    assert not _native._tools_unsupported(TimeoutError("read timed out"))
    assert not _native._tools_unsupported(ConnectionError("connection refused"))
    # a GENERIC 400 (invalid_request_error) echoing the tools schema must NOT be
    # treated as a capability rejection — that permanently disabled native.
    assert not _native._tools_unsupported(RuntimeError(
        'invalid_request_error: unknown parameter "temperature"; function schema echoed'))


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


def test_tools_unsupported_reads_http_body():
    import io
    import urllib.error
    # HTTP 400 whose reason is ONLY in the body — str(exc) is just "HTTP Error 400";
    # the self-heal must read the body or it never fires (the critical bug).
    body = b'{"error":{"message":"this model does not support tools"}}'
    exc = urllib.error.HTTPError("http://x", 400, "Bad Request", {}, io.BytesIO(body))
    assert _native._tools_unsupported(exc) is True
    # a 5xx / 429 is transient — NEVER a definitive tools-rejection, even if the
    # body mentions tools (must not disable native process-wide).
    for code in (500, 503, 429):
        e = urllib.error.HTTPError("http://x", code, "busy", {},
                                   io.BytesIO(b"tools not supported"))
        assert _native._tools_unsupported(e) is False


def _http_err(code, body):
    import io
    import urllib.error
    e = urllib.error.HTTPError(
        "http://x", code, "Bad Request", {}, io.BytesIO(body.encode()))
    e._aiforge_body = body.encode()      # stash so body-readers see it
    return e


def test_round10_tool_choice_only_rejection_not_treated_as_no_tools():
    # A server that does native FC with tool_choice="auto" but rejects the
    # forced "required" mode the probe uses must NOT be permanently disabled.
    exc = _http_err(400, 'Invalid tool_choice: "required" is not supported')
    assert _native._rejects_only_tool_choice(exc) is True


def test_round10_probe_stays_optimistic_on_tool_choice_rejection(monkeypatch):
    _native.reset_native_cache()
    exc = _http_err(400, 'tool_choice "required" not supported')

    def boom(*a, **k):
        raise exc
    from aiforge_core.llm import client
    monkeypatch.setattr(client, "complete_raw", boom)
    monkeypatch.setattr(_native, "_model_for", lambda role: "m-x")
    assert _native._probe_native("planner") is True     # optimistic
    assert "m-x" not in _native._NATIVE_CACHE            # NOT cached-disabled


def test_round11_runtime_fn_not_disabled_on_tool_choice_rejection(monkeypatch):
    # Symmetric to the probe: the RUNTIME complete_fn must not permanently
    # disable native when a server rejects only the tool_choice parameter.
    _native.reset_native_cache()
    from aiforge_core.llm import client
    monkeypatch.setattr(_native, "_model_for", lambda role: "m-tc")

    def raise_tc(*a, **k):
        raise _http_err(400, 'tool_choice "auto" is not supported here')
    monkeypatch.setattr(client, "complete_raw", raise_tc)
    monkeypatch.setattr(client, "complete", lambda role, convo: "TEXT_FALLBACK")

    fn = _native.make_native_complete_fn()
    out = fn("planner", [{"role": "user", "content": "hi"}])
    assert out == "TEXT_FALLBACK"                 # text this turn
    assert _native._NATIVE_CACHE.get("m-tc") is not False   # NOT disabled
