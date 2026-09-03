"""web_crawl — egress gate, url validation, fallback engine, dossier layout."""
from __future__ import annotations

import json
import os

import pytest

from aiforge_core.runtime.tools import web_ingest


@pytest.fixture
def workdir(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_ALLOW_WEB_FETCH", "1")
    monkeypatch.delenv("AIFORGE_WEB_SEARCH_DISABLE", raising=False)
    return tmp_path


def test_gated_off_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("AIFORGE_ALLOW_WEB_FETCH", raising=False)
    r = web_ingest.web_crawl({"url": "https://example.com"}, None)
    assert not r["ok"]
    assert "AIFORGE_ALLOW_WEB_FETCH" in r["error"]


def test_rejects_bad_urls(workdir):
    assert not web_ingest.web_crawl({}, None)["ok"]
    assert not web_ingest.web_crawl({"url": "file:///etc/passwd"}, None)["ok"]
    assert not web_ingest.web_crawl({"url": "ftp://x"}, None)["ok"]


def test_fallback_engine_saves_dossier(workdir, monkeypatch):
    # force the plain-fetch engine and stub the network
    monkeypatch.setenv("AIFORGE_WEB_CRAWLER", "fallback")
    monkeypatch.setattr(
        "aiforge_core.runtime.tools.web_fetch._fetch_readable",
        lambda url, max_chars: {"ok": True, "url": url,
                                "title": "Doc Title", "text": "hello world",
                                "truncated": False})
    r = web_ingest.web_crawl(
        {"url": "https://docs.example.com/guide/intro"}, None)
    assert r["ok"]
    assert r["engine"] == "fetch"
    assert r["title"] == "Doc Title"
    assert os.path.isfile(r["path"])
    body = open(r["path"], encoding="utf-8").read()
    assert "hello world" in body
    assert "docs.example.com" in r["path"]
    # dossier lands under work/web/<slug>/ with a meta.json sibling
    assert f"{os.sep}web{os.sep}" in r["path"]
    meta = json.load(open(os.path.join(os.path.dirname(r["path"]),
                                       "meta.json"), encoding="utf-8"))
    assert meta["url"].endswith("/guide/intro")
    assert meta["engine"] == "fetch"


def test_fetch_failure_propagates(workdir, monkeypatch):
    monkeypatch.setenv("AIFORGE_WEB_CRAWLER", "fallback")
    monkeypatch.setattr(
        "aiforge_core.runtime.tools.web_fetch._fetch_readable",
        lambda url, max_chars: {"ok": False, "error": "boom"})
    r = web_ingest.web_crawl({"url": "https://example.com"}, None)
    assert not r["ok"]
    assert r["error"] == "boom"


def test_slug_is_filesystem_safe():
    s = web_ingest._slug_for("https://a.b/c/d?x=1#f")
    assert "/" not in s
    assert "?" not in s
    assert s


def test_slug_distinguishes_query_strings():
    """pageId=123 vs pageId=456 must NOT overwrite each other's dossier."""
    a = web_ingest._slug_for("https://w.host/pages/viewpage.action?pageId=123")
    b = web_ingest._slug_for("https://w.host/pages/viewpage.action?pageId=456")
    assert a != b


def test_url_credentials_never_persisted(workdir, monkeypatch):
    monkeypatch.setenv("AIFORGE_WEB_CRAWLER", "fallback")
    monkeypatch.setattr(
        "aiforge_core.runtime.tools.web_fetch._fetch_readable",
        lambda url, max_chars: {"ok": True, "url": url, "title": "t",
                                "text": "body", "truncated": False})
    r = web_ingest.web_crawl(
        {"url": "https://user:s3cret@h.io/doc?api_token=abc123"}, None)
    assert r["ok"]
    assert "s3cret" not in r["path"]
    assert "s3cret" not in r["url"]
    blob = open(r["path"], encoding="utf-8").read() + \
        open(os.path.join(os.path.dirname(r["path"]), "meta.json"),
             encoding="utf-8").read()
    assert "s3cret" not in blob
    assert "abc123" not in blob
    assert "REDACTED" in blob      # token PARAM kept, value scrubbed


def test_sanctioned_no_longer_bypasses_the_fetch_gate(monkeypatch, tmp_path):
    """INVERTED on purpose (2026-09-03). `sanctioned: True` used to skip the
    AIFORGE_ALLOW_WEB_FETCH gate for "the researcher wrapper" — but the doer
    wrapper passed it unconditionally and web_crawl is in the BASE tool list,
    so every role crawled any URL with the operator's switch off. With web
    search removed, that was the widest remaining way to put a query string on
    the wire. The flag is now accepted and ignored."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("AIFORGE_ALLOW_WEB_FETCH", raising=False)
    monkeypatch.setenv("AIFORGE_WEB_CRAWLER", "fallback")
    monkeypatch.setattr(
        "aiforge_core.runtime.tools.web_fetch._fetch_readable",
        lambda url, max_chars: {"ok": True, "url": url, "title": "t",
                                "text": "body", "truncated": False})
    monkeypatch.delenv("AIFORGE_WEB_FETCH_DISABLE", raising=False)
    monkeypatch.delenv("AIFORGE_WEB_SEARCH_DISABLE", raising=False)
    assert not web_ingest.web_crawl({"url": "https://x.io/d"}, None)["ok"]
    assert not web_ingest.web_crawl(
        {"url": "https://x.io/d", "sanctioned": True}, None)["ok"]
    # ...and with the switch ON, both forms work — the flag is simply inert.
    monkeypatch.setenv("AIFORGE_ALLOW_WEB_FETCH", "1")
    assert web_ingest.web_crawl({"url": "https://x.io/d"}, None)["ok"]
    assert web_ingest.web_crawl(
        {"url": "https://x.io/d", "sanctioned": True}, None)["ok"]
