"""Jira/Confluence default project/space: persist once, auto-fill later.

Covers the user-reported gap "when I say use X as the default project/space it
is not taking it" — the chat tool persists a default and jira_*/confluence_*
scope to it when the caller omits project/space, while an explicit arg/JQL wins.
"""
from __future__ import annotations

import os
import tempfile

import pytest


@pytest.fixture()
def cfg_dir(monkeypatch):
    d = tempfile.mkdtemp()
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", d)
    # Clear any env overrides so the store is the sole source of the default.
    for k in ("JIRA_DEFAULT_PROJECT", "CONFLUENCE_DEFAULT_SPACE"):
        monkeypatch.delenv(k, raising=False)
    return d


def test_set_integration_default_persists_and_reads(cfg_dir):
    from aiforge_core.runtime import chat_agent as ca
    from aiforge_core.runtime.tools import confluence, jira

    assert ca._t_set_integration_default({"tool": "jira", "value": "ENG"}, ".")["ok"]
    assert ca._t_set_integration_default(
        {"tool": "confluence", "value": "DEV"}, ".")["ok"]
    assert jira.default_project() == "ENG"
    assert confluence.default_space() == "DEV"

    bad = ca._t_set_integration_default({"tool": "slack", "value": "x"}, ".")
    assert not bad["ok"]


def test_jira_search_scopes_to_default_project(cfg_dir, monkeypatch):
    from aiforge_core.runtime import chat_agent as ca
    from aiforge_core.runtime.tools import jira

    ca._t_set_integration_default({"tool": "jira", "value": "ENG"}, ".")
    monkeypatch.setenv("JIRA_BASE_URL", "http://x")
    monkeypatch.setenv("JIRA_TOKEN", "t")
    seen: dict = {}
    monkeypatch.setattr(jira, "_request",
                        lambda m, p, **kw: seen.update(kw.get("params") or {})
                        or {"ok": True, "data": {"issues": []}})

    jira.jira_search({"query": "foo"}, ".")
    assert 'project = "ENG"' in seen["jql"]           # default applied

    seen.clear()
    jira.jira_search({"jql": "project = OTHER AND text ~ 'z'"}, ".")
    assert seen["jql"] == "project = OTHER AND text ~ 'z'"   # explicit JQL untouched

    seen.clear()
    jira.jira_search({"query": "foo", "project": "ZZZ"}, ".")
    assert 'project = "ZZZ"' in seen["jql"]           # explicit arg wins over default


def test_confluence_search_scopes_to_default_space(cfg_dir, monkeypatch):
    from aiforge_core.runtime import chat_agent as ca
    from aiforge_core.runtime.tools import confluence

    ca._t_set_integration_default({"tool": "confluence", "value": "DEV"}, ".")
    monkeypatch.setenv("CONFLUENCE_BASE_URL", "http://x")
    monkeypatch.setenv("CONFLUENCE_TOKEN", "t")
    seen: dict = {}
    monkeypatch.setattr(confluence, "_request",
                        lambda m, p, **kw: seen.update(kw.get("params") or {})
                        or {"ok": True, "data": {"results": []}})

    confluence.confluence_search({"query": "bar"}, ".")
    assert 'space = "DEV"' in seen["cql"]             # default applied

    seen.clear()
    confluence.confluence_search({"cql": "space = OTHER AND text ~ 'z'"}, ".")
    assert seen["cql"] == "space = OTHER AND text ~ 'z'"  # explicit CQL untouched
