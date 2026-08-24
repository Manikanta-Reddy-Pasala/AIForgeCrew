"""Web-tool surface (updated 66784fc): web_search + web_crawl are in the BASE
surface — every tool-using agent gets them (gated by AIFORGE_ALLOW_WEB_FETCH,
SSRF-guarded); web_search is keyless DuckDuckGo. web_READ (ungated raw page
read) stays RESEARCHER-only. fetch_url stays gated."""
from __future__ import annotations
import os
import pytest
from aiforge_core.runtime import doer_tools as dt


def _names(role):
    return {getattr(t, "name", None) or t.func.__name__
            for t in dt.adk_function_tools(role=role)}


def test_researcher_gets_web_tools():
    n = _names("researcher")
    assert {"web_search", "web_read", "web_crawl"} <= n


def test_doer_no_role_gets_base_web_but_not_web_read():
    # role=None returns the full base set — web_search + web_crawl are in it now,
    # but web_read (ungated raw read) stays researcher-only.
    n = _names(None)
    assert "web_search" in n
    assert "web_crawl" in n
    assert "web_read" not in n


def test_planner_gets_web_search_not_web_read():
    # a tool-using role (planner allowlist includes web_search/web_crawl); the
    # ungated web_read is still researcher-only.
    n = _names("planner")
    assert "web_search" in n
    assert "web_crawl" in n
    assert "web_read" not in n


def test_web_read_ungated(monkeypatch):
    # web_read reads without the AIFORGE_ALLOW_WEB_FETCH gate
    monkeypatch.delenv("AIFORGE_ALLOW_WEB_FETCH", raising=False)
    monkeypatch.setattr(dt, "_do_fetch", lambda url: {"ok": True, "url": url, "body": "x"})
    r = dt.web_read("http://example.com")
    assert r["ok"] is True


def test_fetch_url_still_gated(monkeypatch):
    monkeypatch.delenv("AIFORGE_ALLOW_WEB_FETCH", raising=False)
    called = {"n": 0}
    monkeypatch.setattr(dt, "_do_fetch", lambda url: called.__setitem__("n", called["n"] + 1) or {"ok": True})
    r = dt.fetch_url("http://example.com")
    assert r["ok"] is False
    assert called["n"] == 0
    monkeypatch.setenv("AIFORGE_ALLOW_WEB_FETCH", "1")
    assert dt.fetch_url("http://example.com")["ok"] is True
