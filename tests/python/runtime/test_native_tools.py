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
    assert parsed["tool"] == "grep"
    assert parsed["args"] == {"pattern": "TODO"}


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
        assert parsed["tool"] == "list_dir"
        assert parsed["args"] == {}


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
    from aiforge_core.runtime.chat_agent._tools._schemas import LOOP_TOOLS
    # EVERY registry tool is exposed natively (rich or permissive), and every
    # native name dispatches through the real registry or the loop itself —
    # no orphans either way.
    assert set(NATIVE_TOOL_NAMES) == set(TOOLS) | set(LOOP_TOOLS)
    # schemas are well-formed OpenAI function tools
    for s in NATIVE_TOOL_SCHEMAS:
        assert s["type"] == "function"
        fn = s["function"]
        assert fn["name"]
        assert fn["parameters"]["type"] == "object"
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


def test_what_the_model_said_with_a_tool_call_is_kept_as_its_thought():
    """The narration streamed to the user before the tool ran; the step used to
    drop it, so it vanished from the chat and was never saved."""
    msg = {"role": "assistant",
           "content": "Let me read the config first.\nNote: ACTION: rm is not it",
           "tool_calls": [{"function": {"name": "file_read",
                                        "arguments": {"path": "app.yaml"}}}]}
    parsed = _parse(_native._synth_step(msg))
    assert parsed["kind"] == "action" and parsed["tool"] == "file_read"
    assert parsed["args"] == {"path": "app.yaml"}
    assert parsed["thought"].startswith("Let me read the config first.")
    assert "rm is not it" in parsed["thought"]          # not cut at "Note:"


def test_narration_does_not_defeat_the_duplicate_call_drop():
    call = {"function": {"name": "file_read", "arguments": {"path": "a.py"}}}
    msg = {"content": "Let me read it.", "tool_calls": [call, call]}
    assert _native._queued_steps(msg) == ([], 0)


def test_a_done_pseudo_call_keeps_no_narration():
    msg = {"content": "All done: fixed X.\n\nDetails", "tool_calls": [
        {"function": {"name": "final_answer", "arguments": {}}}]}
    assert "THOUGHT" not in _native._synth_step(msg)


def test_narration_written_as_text_protocol_keeps_only_the_prose():
    msg = {"content": "THOUGHT: check the config\nACTION: file_read\nARGS_JSON: {}",
           "tool_calls": [{"function": {"name": "file_read",
                                        "arguments": {"path": "a.yaml"}}}]}
    assert _parse(_native._synth_step(msg))["thought"] == "check the config"


def test_a_forced_write_step_offers_only_edit_tools_once(monkeypatch):
    """click af6: 135 shell commands, two "write the change" notes ignored.
    A short burst keeps edit-only for FORCE_WRITE_BURST calls, then restores
    the full tool list (one micro-edit used to unlock another shell loop)."""
    _native.reset_native_cache()
    monkeypatch.setattr(_native, "_model_for", lambda role: "m-fw")
    from aiforge_core.llm import client
    seen = []

    def raw(role, msgs, tools=None, tool_choice=None):
        seen.append(({(t.get("function") or {}).get("name") for t in tools}, tool_choice))
        # a real edit spends the burst step (content-only would not)
        return {"role": "assistant", "content": "",
                "tool_calls": [{"id": "1", "type": "function",
                                "function": {"name": "file_patch",
                                             "arguments": "{}"}}]}
    monkeypatch.setattr(client, "complete_raw", raw)
    fn = _native.make_native_complete_fn(session_id=77)
    _native.force_write_next(77)
    for _ in range(_native.FORCE_WRITE_BURST):
        fn("chat", [{"role": "user", "content": "fix the bug in a.py"}])
    fn("chat", [{"role": "user", "content": "fix the bug in a.py"}])
    assert len(seen) == _native.FORCE_WRITE_BURST + 1
    for tools, choice in seen[:-1]:
        assert choice == "required" and tools and tools <= _native.WRITE_TOOLS
    last_tools, last_choice = seen[-1]
    assert last_choice == "auto" and "run_command" in last_tools


def test_forced_write_does_not_spend_burst_on_plan_progress(monkeypatch):
    """boc4a: plan_progress came back on a forced step; zero files changed."""
    _native.reset_native_cache()
    monkeypatch.setattr(_native, "_model_for", lambda role: "m-fw2")
    from aiforge_core.llm import client
    calls = {"n": 0}

    def raw(role, msgs, tools=None, tool_choice=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"role": "assistant", "content": "",
                    "tool_calls": [{"id": "1", "type": "function",
                                    "function": {"name": "plan_progress",
                                                 "arguments": "{}"}}]}
        return {"role": "assistant", "content": "",
                "tool_calls": [{"id": "2", "type": "function",
                                "function": {"name": "file_patch",
                                             "arguments": "{}"}}]}
    monkeypatch.setattr(client, "complete_raw", raw)
    fn = _native.make_native_complete_fn(session_id=78)
    _native.force_write_next(78)
    step = fn("chat", [{"role": "user", "content": "fix it"}])
    assert calls["n"] == 2                          # retried once
    # one real edit spent the default burst of 1
    assert 78 not in _native._FORCE_WRITE
    assert 78 not in _native._FORCE_WRITE_LEFT
    assert "file_patch" in str(step)
    _native.clear_force_write(78)


def test_forced_write_spends_burst_after_failed_retry(monkeypatch):
    """boc5a: read_lines forever never spent the burst → 40 forced steps."""
    _native.reset_native_cache()
    monkeypatch.setattr(_native, "_model_for", lambda role: "m-fw3")
    from aiforge_core.llm import client

    def raw(role, msgs, tools=None, tool_choice=None):
        return {"role": "assistant", "content": "",
                "tool_calls": [{"id": "1", "type": "function",
                                "function": {"name": "read_lines",
                                             "arguments": "{}"}}]}
    monkeypatch.setattr(client, "complete_raw", raw)
    fn = _native.make_native_complete_fn(session_id=79)
    _native.force_write_next(79)
    step = fn("chat", [{"role": "user", "content": "fix it"}])
    # failed retry still spends the default burst of 1 so it cannot loop forever
    assert 79 not in _native._FORCE_WRITE
    assert 79 not in _native._FORCE_WRITE_LEFT
    assert "tool_calls" not in str(step) or "read_lines" not in str(
        getattr(step, "get", lambda *a: "")("tool_calls") or "")
    # non-edit calls must not run
    assert step is not None
    # boc7a: must not surface as scaffolding-only (text-protocol nudge)
    assert "protocol scaffolding" not in step
    assert "forced write skipped" in step or "THOUGHT:" in step
    from aiforge_core.runtime.chat_agent._prompt import _parse
    parsed = _parse(step)
    assert parsed.get("kind") == "continue"
    _native.clear_force_write(79)


def test_a_forced_final_step_allows_no_tool_once(monkeypatch):
    _native.reset_native_cache()
    monkeypatch.setattr(_native, "_model_for", lambda role: "m-ff")
    from aiforge_core.llm import client
    seen = []

    def raw(role, msgs, tools=None, tool_choice=None):
        seen.append(tool_choice)
        return {"role": "assistant", "content": "Done."}
    monkeypatch.setattr(client, "complete_raw", raw)
    fn = _native.make_native_complete_fn(session_id=88)
    _native.force_final_next(88)
    fn("chat", [{"role": "user", "content": "x"}])
    fn("chat", [{"role": "user", "content": "x"}])
    assert seen == ["none", "auto"]
