"""Which ROLE gets which web tool. web_crawl is in the BASE surface; web_read
stays RESEARCHER-only. Both are gated now — see test_egress_switches.py for the
switches themselves; this file is only about role scoping. There is NO web
SEARCH tool on any role (removed 2026-09-03)."""
from __future__ import annotations
import os
import pytest
from aiforge_core.runtime import doer_tools as dt


def _names(role):
    return {getattr(t, "name", None) or t.func.__name__
            for t in dt.adk_function_tools(role=role)}


def test_researcher_gets_web_tools():
    n = _names("researcher")
    assert {"web_read", "web_crawl"} <= n
    assert "web_search" not in n


def test_doer_no_role_gets_base_web_but_not_web_read():
    # role=None returns the full base set — web_crawl is in it, but web_read
    # (raw page read) stays researcher-only and search does not exist.
    n = _names(None)
    assert "web_search" not in n
    assert "web_crawl" in n
    assert "web_read" not in n


def test_planner_gets_web_crawl_not_web_read_or_search():
    # a tool-using role (planner allowlist includes web_crawl); the
    # web_read is still researcher-only, and search exists nowhere.
    n = _names("planner")
    assert "web_search" not in n
    assert "web_crawl" in n
    assert "web_read" not in n


def test_web_read_is_gated_like_everything_else(monkeypatch):
    """This test used to be `test_web_read_ungated` and asserted the OPPOSITE.
    web_read was exempt from the fetch switch so the researcher's search→read
    flow would work; search is gone, and an unattended pre-planner role with
    gate-free egress was then the widest hole in the system."""
    monkeypatch.delenv("AIFORGE_ALLOW_WEB_FETCH", raising=False)
    monkeypatch.delenv("AIFORGE_WEB_FETCH_DISABLE", raising=False)
    monkeypatch.delenv("AIFORGE_WEB_SEARCH_DISABLE", raising=False)
    reached = {"n": 0}
    monkeypatch.setattr(dt, "_do_fetch",
                        lambda url: reached.__setitem__("n", 1) or {"ok": True})
    assert dt.web_read("https://example.com")["ok"] is False
    assert reached["n"] == 0, "web_read reached the network with the switch off"
    monkeypatch.setenv("AIFORGE_ALLOW_WEB_FETCH", "1")
    assert dt.web_read("https://example.com")["ok"] is True


def test_fetch_url_still_gated(monkeypatch):
    monkeypatch.delenv("AIFORGE_ALLOW_WEB_FETCH", raising=False)
    called = {"n": 0}
    monkeypatch.setattr(dt, "_do_fetch", lambda url: called.__setitem__("n", called["n"] + 1) or {"ok": True})
    r = dt.fetch_url("https://example.com")
    assert r["ok"] is False
    assert called["n"] == 0
    monkeypatch.setenv("AIFORGE_ALLOW_WEB_FETCH", "1")
    assert dt.fetch_url("https://example.com")["ok"] is True
