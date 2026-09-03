"""The chat web-intent detector — it now flags a live-web ask so the model is
told it CANNOT look it up (web search was removed; see tools/web_fetch.py)."""
from __future__ import annotations


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
    # a URL already routes to web_crawl — no 'cannot look it up' notice needed
    assert not _has_web_intent("crawl https://redis.io/download for me")
    # no web signal
    assert not _has_web_intent("rename this variable to foo")
    assert not _has_web_intent("")
