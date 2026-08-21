"""Hardened web_search: html→lite fallback + retry, and the chat web-intent
directive that forces web_search on models that would answer from memory."""
from __future__ import annotations

import urllib.error

from aiforge_core.runtime.tools import web_search as ws


_LITE_BODY = (
    '<html><body><table>'
    '<a rel="nofollow" href="https://redis.io/download" class="result-link">'
    'Redis Downloads</a>'
    '<a rel="nofollow" href="https://github.com/redis/redis" class="result-link">'
    'redis/redis</a>'
    '</table></body></html>')

_HTML_BODY = (
    '<a class="result__a" href="https://x.io/a">Title A</a>'
    '<a class="result__snippet" href="https://x.io/a">snippet a</a>')


def test_parse_lite_extracts_result_links():
    out = ws._parse_lite(_LITE_BODY, limit=5)
    assert [r["url"] for r in out] == [
        "https://redis.io/download", "https://github.com/redis/redis"]
    assert out[0]["title"] == "Redis Downloads"


def test_parse_html_pairs_snippets():
    out = ws._parse_html(_HTML_BODY, limit=5)
    assert out == [{"title": "Title A", "url": "https://x.io/a",
                    "snippet": "snippet a"}]


def test_web_search_falls_back_to_lite_when_html_empty(monkeypatch):
    monkeypatch.delenv("AIFORGE_WEB_SEARCH_DISABLE", raising=False)
    monkeypatch.setattr(ws, "_api_search", lambda q, k: None)
    calls = {"html": 0, "lite": 0}

    def fake_get_retry(url, *, data=None, tries=2, verified=None):
        if url == ws._DDG_HTML:
            calls["html"] += 1
            return "<html>no results here</html>"      # parses to []
        calls["lite"] += 1
        return _LITE_BODY
    monkeypatch.setattr(ws, "_get_retry", fake_get_retry)
    r = ws.web_search({"query": "redis latest", "limit": 3})
    assert r["ok"] and r["provider"] == "ddg-lite"
    assert len(r["results"]) == 2 and calls["html"] == 1 and calls["lite"] == 1


def test_web_search_lite_fallback_on_html_error(monkeypatch):
    monkeypatch.delenv("AIFORGE_WEB_SEARCH_DISABLE", raising=False)
    monkeypatch.setattr(ws, "_api_search", lambda q, k: None)

    def fake_get_retry(url, *, data=None, tries=2, verified=None):
        if url == ws._DDG_HTML:
            raise urllib.error.URLError("boom")
        return _LITE_BODY
    monkeypatch.setattr(ws, "_get_retry", fake_get_retry)
    r = ws.web_search({"query": "x", "limit": 5})
    assert r["ok"] and r["provider"] == "ddg-lite" and len(r["results"]) == 2


def test_web_search_reports_error_only_when_both_fail(monkeypatch):
    monkeypatch.delenv("AIFORGE_WEB_SEARCH_DISABLE", raising=False)
    monkeypatch.setattr(ws, "_api_search", lambda q, k: None)

    def boom(url, *, data=None, tries=2, verified=None):
        raise urllib.error.URLError("down")
    monkeypatch.setattr(ws, "_get_retry", boom)
    r = ws.web_search({"query": "x"})
    assert r["ok"] is False and "down" in r["error"]


def test_get_retry_recovers_on_second_try(monkeypatch):
    seq = [urllib.error.URLError("transient"), "OK BODY"]

    def flaky(url, *, data=None, verified=None):
        v = seq.pop(0)
        if isinstance(v, Exception):
            raise v
        return v
    monkeypatch.setattr(ws, "_get", flaky)
    assert ws._get_retry("https://x", tries=2) == "OK BODY"


# ── chat web-intent directive ────────────────────────────────────────────

def test_web_intent_detects_search_phrasings():
    from aiforge_core.runtime.chat_agent import _has_web_intent
    for p in ["search the web for langfuse version",
              "what's the latest version of bun",
              "look it up online",
              "find recent news on MCP",
              "current version of redis?"]:
        assert _has_web_intent(p), p


def test_web_intent_ignores_url_and_plain_text():
    from aiforge_core.runtime.chat_agent import _has_web_intent
    # a URL already routes to web_crawl — don't force web_search
    assert not _has_web_intent("crawl https://redis.io/download for me")
    # no web signal
    assert not _has_web_intent("rename this variable to foo")
    assert not _has_web_intent("")
