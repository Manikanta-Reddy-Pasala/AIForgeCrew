"""web_fetch — page read mocked at the urllib layer. (The search half of
this module was deleted; see test_no_web_search.py for the regression pins.)"""
from __future__ import annotations

import pytest

from aiforge_core.runtime.tools import web_fetch as ws


@pytest.fixture(autouse=True)
def _allow_web_fetch(monkeypatch):
    # This file exercises fetch MECHANICS; the AIFORGE_ALLOW_WEB_FETCH
    # lockdown gate is covered separately in test_web_fetch_gated_chat.py.
    monkeypatch.setenv("AIFORGE_ALLOW_WEB_FETCH", "1")


class _Resp:
    def __init__(self, body: bytes):
        self._b = body

    def read(self, n=-1):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _mock(monkeypatch, body: str):
    """Patch the OPENER, not urllib.request.urlopen.

    The module builds its own opener so every redirect hop can be re-guarded;
    patching urlopen therefore stops intercepting anything and the test fetches
    the real internet — which is how these two cases started passing against
    a live https://ex.com instead of the fixture.
    """
    seen = {}

    class _FakeOpener:
        def open(self, req, timeout=None):
            seen["url"] = req.full_url
            seen["method"] = req.get_method()
            seen["ua"] = dict(req.header_items()).get("User-agent")
            return _Resp(body.encode())

    monkeypatch.setattr(ws, "_opener", lambda ctx: _FakeOpener())
    return seen


def test_fetch_strips_html_and_scripts(monkeypatch):
    page = ("<html><head><title>My Page</title><style>.x{}</style></head>"
            "<body><script>evil()</script><h1>Hello</h1>"
            "<p>World text here.</p></body></html>")
    _mock(monkeypatch, page)
    out = ws.web_fetch({"url": "https://ex.com", "max_chars": 1000})
    assert out["ok"]
    assert out["title"] == "My Page"
    assert "Hello" in out["text"]
    assert "World text here." in out["text"]
    assert "evil()" not in out["text"]
    assert ".x{}" not in out["text"]


def test_fetch_truncates(monkeypatch):
    _mock(monkeypatch, "<body>" + ("ab " * 100) + "</body>")
    out = ws.web_fetch({"url": "https://ex.com", "max_chars": 20})
    assert out["truncated"]
    assert len(out["text"]) == 20


def test_fetch_rejects_non_http():
    assert ws.web_fetch({"url": "file:///etc/passwd"})["error"] == "url must be http(s)"


def test_fetch_requires_url():
    assert ws.web_fetch({})["error"] == "missing 'url'"


