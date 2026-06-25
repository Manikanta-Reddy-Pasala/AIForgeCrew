"""web_search / web_fetch — DDG HTML parsing mocked at the urllib layer."""
from __future__ import annotations

import pytest

from aiforge_core.runtime.tools import web_search as ws


class _Resp:
    def __init__(self, body: bytes):
        self._b = body

    def read(self, n=-1):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_DDG_HTML = (
    '<div class="result">'
    '<a class="result__a" rel="nofollow" '
    'href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fdoc&rut=x">'
    'Example <b>Doc</b></a>'
    '<a class="result__snippet" href="...">The <b>answer</b> is 42.</a>'
    '</div>'
    '<div class="result">'
    '<a class="result__a" href="https://second.example/page">Second</a>'
    '<a class="result__snippet">second snippet</a>'
    '</div>'
)


def _mock(monkeypatch, body: str):
    seen = {}

    def fake_urlopen(req, timeout=None, context=None):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        seen["ua"] = dict(req.header_items()).get("User-agent")
        return _Resp(body.encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return seen


def test_search_parses_and_decodes_redirect(monkeypatch):
    seen = _mock(monkeypatch, _DDG_HTML)
    out = ws.web_search({"query": "what is the answer", "limit": 5})
    assert out["ok"]
    r0 = out["results"][0]
    assert r0["title"] == "Example Doc"                      # tags stripped
    assert r0["url"] == "https://example.com/doc"            # uddg decoded
    assert r0["snippet"] == "The answer is 42."
    assert out["results"][1]["url"] == "https://second.example/page"
    assert "duckduckgo.com/html" in seen["url"]
    assert seen["method"] == "POST" and seen["ua"]           # UA sent


def test_search_limit(monkeypatch):
    _mock(monkeypatch, _DDG_HTML)
    out = ws.web_search({"query": "x", "limit": 1})
    assert len(out["results"]) == 1


def test_search_requires_query():
    assert ws.web_search({})["error"] == "missing 'query'"


def test_search_empty_results(monkeypatch):
    _mock(monkeypatch, "<html>nothing</html>")
    out = ws.web_search({"query": "zzz"})
    assert out["ok"] and out["results"] == []


def test_search_disabled(monkeypatch):
    monkeypatch.setenv("AIFORGE_WEB_SEARCH_DISABLE", "1")
    assert ws.web_search({"query": "x"})["error"] == "web_search_disabled"


def test_fetch_strips_html_and_scripts(monkeypatch):
    page = ("<html><head><title>My Page</title><style>.x{}</style></head>"
            "<body><script>evil()</script><h1>Hello</h1>"
            "<p>World text here.</p></body></html>")
    _mock(monkeypatch, page)
    out = ws.web_fetch({"url": "https://ex.com", "max_chars": 1000})
    assert out["ok"] and out["title"] == "My Page"
    assert "Hello" in out["text"] and "World text here." in out["text"]
    assert "evil()" not in out["text"] and ".x{}" not in out["text"]


def test_fetch_truncates(monkeypatch):
    _mock(monkeypatch, "<body>" + ("ab " * 100) + "</body>")
    out = ws.web_fetch({"url": "https://ex.com", "max_chars": 20})
    assert out["truncated"] and len(out["text"]) == 20


def test_fetch_rejects_non_http():
    assert ws.web_fetch({"url": "file:///etc/passwd"})["error"] == "url must be http(s)"


def test_fetch_requires_url():
    assert ws.web_fetch({})["error"] == "missing 'url'"


def test_search_block_without_snippet_does_not_bleed(monkeypatch):
    # First result has NO snippet; the second result's snippet must NOT be
    # mis-attributed to the first (the old two-findall zip would desync here).
    page = (
        '<a class="result__a" href="https://first.example/x">First</a>'
        '<a class="result__a" href="https://second.example/y">Second</a>'
        '<a class="result__snippet">snippet for second</a>'
    )
    _mock(monkeypatch, page)
    out = ws.web_search({"query": "x"})
    assert out["results"][0]["url"] == "https://first.example/x"
    assert out["results"][0]["snippet"] == ""                    # no bleed
    assert out["results"][1]["snippet"] == "snippet for second"
