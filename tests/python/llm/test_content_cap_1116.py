"""A tool-offering stream whose reply text runs long with no tool call is cut
and asked again once with a note; answers without tools are never capped."""
import pytest

from aiforge_core.llm.client import _http, _http_stream
from aiforge_core.llm.client._http_stream import ContentOverCap, _StreamAssembler


def _chunk(text=None, tool=None):
    d = {}
    if text:
        d["content"] = text
    if tool:
        d["tool_calls"] = [tool]
    return {"choices": [{"delta": d}]}


def test_long_text_without_tool_call_raises():
    tok = _http_stream.CONTENT_CAP_CHARS.set(100)
    try:
        a = _StreamAssembler(lambda k, t: None)
        a.feed(_chunk("x" * 60))
        with pytest.raises(ContentOverCap):
            a.feed(_chunk("y" * 60))
    finally:
        _http_stream.CONTENT_CAP_CHARS.reset(tok)


def test_text_after_a_tool_call_started_is_not_capped():
    tok = _http_stream.CONTENT_CAP_CHARS.set(100)
    try:
        a = _StreamAssembler(lambda k, t: None)
        a.feed(_chunk(tool={"index": 0, "function": {"name": "read", "arguments": ""}}))
        a.feed(_chunk("z" * 500))
        assert a.body()["choices"][0]["message"]["tool_calls"]
    finally:
        _http_stream.CONTENT_CAP_CHARS.reset(tok)


def test_cap_only_on_tool_requests_and_never_on_the_reask(monkeypatch):
    monkeypatch.delenv("AIFORGE_LLM_CONTENT_CAP_CHARS", raising=False)
    assert _http._content_cap_chars(b'{"messages":[],"tools":[{}]}') == 16000
    assert _http._content_cap_chars(b'{"messages":[]}') == 0
    tok = _http._UNCAPPED.set(True)
    try:
        assert _http._content_cap_chars(b'{"tools":[{}]}') == 0
    finally:
        _http._UNCAPPED.reset(tok)
    monkeypatch.setenv("AIFORGE_LLM_CONTENT_CAP_CHARS", "0")
    assert _http._content_cap_chars(b'{"tools":[{}]}') == 0


def test_native_post_reasks_once_with_the_note(monkeypatch):
    from aiforge_core.llm import client
    sent = []

    def fake_post(ep, payload, timeout_s, **kw):
        sent.append(payload)
        if len(sent) == 1:
            raise ContentOverCap("long")
        return {"ok": True}

    monkeypatch.setattr(client, "_post_with_retry", fake_post)
    monkeypatch.setattr(client, "_build_body",
                        lambda ep, msgs, *a: repr(msgs).encode())
    ep = type("E", (), {"model": "m", "base_url": "u"})()
    out = client._native_post(ep, [{"role": "user", "content": "fix it"}], 0, None, None,
                              {}, b"first", 30, "chat", None)
    assert out == {"ok": True} and len(sent) == 2
    assert b"cut off" in sent[1] and b"fix it" in sent[1]
