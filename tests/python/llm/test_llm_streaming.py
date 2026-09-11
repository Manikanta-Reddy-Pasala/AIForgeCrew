"""Chat replies stream: the answer used to arrive in one piece when the call
finished. The client streams when a delta sink is bound and reassembles the
chunks into the normal completion body, so everything above it is unchanged.
"""
import io
import json
import urllib.error

import pytest

from aiforge_core.llm.client import _http


def _chunk(**delta):
    return {"id": "c1", "model": "m", "choices": [{"index": 0, "delta": delta}]}


def test_chunks_reassemble_into_a_normal_body():
    got = []
    asm = _http._StreamAssembler(lambda k, t: got.append((k, t)))
    asm.feed(_chunk(reasoning_content="think "))
    asm.feed(_chunk(content="Hel"))
    asm.feed(_chunk(content="lo"))
    asm.feed({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
              "usage": {"completion_tokens": 3}})
    body = asm.body()
    msg = body["choices"][0]["message"]
    assert msg["content"] == "Hello"
    assert msg["reasoning_content"] == "think "
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"] == {"completion_tokens": 3}
    assert got == [("reasoning", "think "), ("content", "Hel"), ("content", "lo")]


def test_tool_call_fragments_are_joined():
    asm = _http._StreamAssembler(lambda k, t: None)
    asm.feed(_chunk(tool_calls=[{"index": 0, "id": "t1", "function": {"name": "file_", "arguments": '{"pa'}}]))
    asm.feed(_chunk(tool_calls=[{"index": 0, "function": {"name": "read", "arguments": 'th":"a"}'}}]))
    msg = asm.body()["choices"][0]["message"]
    assert msg["tool_calls"] == [{"id": "t1", "type": "function",
                                  "function": {"name": "file_read", "arguments": '{"path":"a"}'}}]
    assert msg["content"] is None


def test_a_broken_sink_never_fails_the_call():
    def boom(k, t):
        raise RuntimeError("ui gone")
    asm = _http._StreamAssembler(boom)
    asm.feed(_chunk(content="x"))
    assert asm.body()["choices"][0]["message"]["content"] == "x"


class _Resp:
    def __init__(self, lines, ctype="text/event-stream", status=200):
        self._lines = [ln.encode() for ln in lines]
        self.status, self.reason, self.headers = status, "OK", {}
        self._ctype = ctype

    def getheader(self, name, default=None):
        return self._ctype if name.lower() == "content-type" else default

    def readline(self):
        return self._lines.pop(0) if self._lines else b""

    def read(self):
        return b"".join(self._lines)


class _Conn:
    def __init__(self, resp):
        self.resp = resp

    def getresponse(self):
        return self.resp


def test_an_sse_response_is_read_as_it_arrives():
    got = []
    lines = [f"data: {json.dumps(_chunk(content='a'))}\n", "\n",
             ": keep-alive\n", f"data: {json.dumps(_chunk(content='b'))}\n",
             "data: [DONE]\n"]
    body = _http._read_sse_response(_Conn(_Resp(lines)), "u", lambda k, t: got.append((k, t)))
    assert body["choices"][0]["message"]["content"] == "ab"
    assert got == [("start", ""), ("content", "a"), ("content", "b")]


def test_a_server_that_ignores_stream_is_read_as_json():
    plain = json.dumps({"choices": [{"message": {"content": "whole"}}]})
    body = _http._read_sse_response(
        _Conn(_Resp([plain], ctype="application/json")), "u", lambda k, t: None)
    assert body["choices"][0]["message"]["content"] == "whole"


def test_a_refused_stream_is_retried_unstreamed(monkeypatch):
    calls = []

    def once(ep, payload, timeout_s, cancel, sent, sink):
        calls.append(("stream" in json.loads(payload), sink is not None))
        if sink is not None:
            raise urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(b""))
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(_http, "_post_cancellable_once", once)
    tok = _http._DELTA_SINK.set(lambda k, t: None)
    try:
        ep = type("E", (), {"base_url": "http://x/v1"})()
        out = _http._post_cancellable(ep, json.dumps({"model": "m"}).encode(), 5, None)
    finally:
        _http._DELTA_SINK.reset(tok)
    assert out["choices"][0]["message"]["content"] == "ok"
    assert calls == [(True, True), (False, False)]


def test_no_sink_means_no_stream(monkeypatch):
    seen = []
    monkeypatch.setattr(_http, "_post_cancellable_once",
                        lambda ep, payload, *a: seen.append(json.loads(payload)) or {})
    _http._post_cancellable(type("E", (), {"base_url": "x"})(), b'{"model":"m"}', 5, None)
    assert "stream" not in seen[0]


@pytest.mark.parametrize("status", [401, 500])
def test_other_http_errors_are_not_retried_here(monkeypatch, status):
    def once(*a):
        raise urllib.error.HTTPError("u", status, "x", {}, io.BytesIO(b""))
    monkeypatch.setattr(_http, "_post_cancellable_once", once)
    tok = _http._DELTA_SINK.set(lambda k, t: None)
    ep = type("E", (), {"base_url": "x"})()
    try:
        with pytest.raises(urllib.error.HTTPError):
            _http._post_cancellable(ep, b"{}", 5, None)
    finally:
        _http._DELTA_SINK.reset(tok)
