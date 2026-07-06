"""llm.client._maybe_compress — library-mode headroom wiring. Compresses
via the local sidecar's /v1/compress when AIFORGE_HEADROOM=1, and is a
strict no-op / soft-fail otherwise so it can never break or stall an LLM
call."""
from __future__ import annotations

import json

import pytest

from aiforge_core.llm import client as c

MSGS = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "hi"},
    {"role": "tool", "tool_call_id": "1", "content": "x" * 5000},
]


def test_disabled_is_passthrough(monkeypatch):
    monkeypatch.delenv("AIFORGE_HEADROOM", raising=False)
    # Must not even attempt a network call when off.
    def _boom(*a, **k):
        raise AssertionError("urlopen must not be called when headroom is off")
    monkeypatch.setattr(c.urllib.request, "urlopen", _boom)
    assert c._maybe_compress(MSGS, "m") is MSGS


def test_enabled_uses_compressed_messages(monkeypatch):
    monkeypatch.setenv("AIFORGE_HEADROOM", "1")
    compressed = [{"role": "tool", "tool_call_id": "1", "content": "SHORT"}]
    captured = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            return json.dumps({"messages": compressed, "tokens_saved": 1200,
                               "compression_ratio": 0.4}).encode()

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        return _Resp()

    monkeypatch.setattr(c.urllib.request, "urlopen", _fake_urlopen)
    out = c._maybe_compress(MSGS, "qwen")
    assert out == compressed
    assert captured["url"].endswith("/v1/compress")
    assert captured["body"]["model"] == "qwen"
    assert captured["body"]["messages"] == MSGS


def test_error_falls_back_to_original(monkeypatch):
    monkeypatch.setenv("AIFORGE_HEADROOM", "1")

    def _fail(*a, **k):
        raise OSError("sidecar down")

    monkeypatch.setattr(c.urllib.request, "urlopen", _fail)
    # Soft-fail: original messages returned, no raise.
    assert c._maybe_compress(MSGS, "m") is MSGS


def test_empty_or_garbage_response_falls_back(monkeypatch):
    monkeypatch.setenv("AIFORGE_HEADROOM", "1")

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps({"messages": []}).encode()  # empty

    monkeypatch.setattr(c.urllib.request, "urlopen", lambda *a, **k: _Resp())
    assert c._maybe_compress(MSGS, "m") is MSGS


def test_empty_input_is_noop(monkeypatch):
    monkeypatch.setenv("AIFORGE_HEADROOM", "1")
    monkeypatch.setattr(c.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call")))
    assert c._maybe_compress([], "m") == []


def test_custom_url_honored(monkeypatch):
    monkeypatch.setenv("AIFORGE_HEADROOM", "1")
    monkeypatch.setenv("AIFORGE_HEADROOM_URL", "http://other-host:9999")
    seen = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps({"messages": MSGS}).encode()

    def _fake(req, timeout=None):
        seen["url"] = req.full_url
        return _Resp()

    monkeypatch.setattr(c.urllib.request, "urlopen", _fake)
    c._maybe_compress(MSGS, "m")
    assert seen["url"] == "http://other-host:9999/v1/compress"
