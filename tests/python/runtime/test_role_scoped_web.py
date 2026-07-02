"""Role-scoped web: only the researcher gets web_search + web_read; the Doer
(no role) and other agents never receive them; fetch_url stays gated."""
from __future__ import annotations
import os
import pytest
from aiforge_core.runtime import doer_tools as dt


def _names(role):
    return {getattr(t, "name", None) or t.func.__name__
            for t in dt.adk_function_tools(role=role)}


def test_researcher_gets_web_tools():
    n = _names("researcher")
    assert "web_search" in n and "web_read" in n


def test_doer_no_role_has_no_web_tools():
    # role=None returns the full base set — which must NOT contain the web tools
    n = _names(None)
    assert "web_search" not in n and "web_read" not in n


def test_other_role_no_web(monkeypatch):
    # a restricted non-researcher role never gets web tools
    n = _names("planner")
    assert "web_search" not in n and "web_read" not in n


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
    assert r["ok"] is False and called["n"] == 0   # gated → no fetch
    monkeypatch.setenv("AIFORGE_ALLOW_WEB_FETCH", "1")
    assert dt.fetch_url("http://example.com")["ok"] is True
