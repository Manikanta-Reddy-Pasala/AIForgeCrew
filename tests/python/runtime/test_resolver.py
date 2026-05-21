from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from aiforge_core.runtime import resolver


def _issue(number: int, title: str, body: str, with_pr: bool = False) -> dict:
    out = {
        "number": number, "title": title, "body": body,
        "user": {"login": "alice"},
        "html_url": f"https://github.com/o/r/issues/{number}",
    }
    if with_pr:
        out["pull_request"] = {"url": "x"}
    return out


def test_issue_to_ticket_shape():
    iss = _issue(42, "Bug in module X", "It crashes when Y happens.")
    t = resolver.issue_to_ticket(iss, project="P")
    assert t["project"] == "P"
    assert t["title"] == "Bug in module X"
    assert "It crashes" in t["body"]
    assert "alice" in t["body"]
    assert t["metadata"]["issue_number"] == 42
    assert t["metadata"]["source"] == "github_issue"


def test_list_open_bot_issues_filters_prs(monkeypatch):
    issues = [
        _issue(1, "real issue", "x"),
        _issue(2, "actually a PR", "x", with_pr=True),
    ]
    monkeypatch.setattr(resolver, "_gh_get", lambda path: issues)
    out = resolver.list_open_bot_issues("o/r", "lbl")
    assert len(out) == 1
    assert out[0]["number"] == 1


def test_list_open_bot_issues_empty_repo():
    out = resolver.list_open_bot_issues("", "lbl")
    assert out == []


def test_list_open_bot_issues_soft_errors(monkeypatch):
    def _boom(path):
        raise OSError("dns dead")
    monkeypatch.setattr(resolver, "_gh_get", _boom)
    out = resolver.list_open_bot_issues("o/r", "lbl")
    assert out == []


def _patch_store(monkeypatch, fake_module):
    """Replace the resolver's lazy ``tickets.store`` import target."""
    import aiforge_core.tickets as tickets_pkg
    monkeypatch.setattr(tickets_pkg, "store", fake_module, raising=False)


def test_resolve_once_creates_tickets(monkeypatch):
    issues = [_issue(1, "task A", "body A"), _issue(2, "task B", "body B")]
    monkeypatch.setattr(resolver, "list_open_bot_issues", lambda *a, **k: issues)

    added: list[dict] = []
    class _FakeStore:
        @staticmethod
        def add(*, project, title, body, metadata):
            added.append({"project": project, "title": title,
                          "body": body, "metadata": metadata})

    _patch_store(monkeypatch, _FakeStore)
    result = resolver.resolve_once(repo="o/r", project="X")
    assert result["ok"]
    assert result["scanned"] == 2
    assert result["created"] == 2
    assert len(added) == 2
    assert added[0]["title"] == "task A"


def test_resolve_once_skips_existing(monkeypatch):
    issues = [_issue(1, "task A", "body A")]
    monkeypatch.setattr(resolver, "list_open_bot_issues", lambda *a, **k: issues)
    class _FakeStore:
        @staticmethod
        def add(**kw):
            raise AssertionError("should not be called when existing found")
        @staticmethod
        def find_by_issue_url(url):
            return {"id": 99}
    _patch_store(monkeypatch, _FakeStore)
    result = resolver.resolve_once(repo="o/r")
    assert result["ok"]
    assert result["created"] == 0
    assert result["skipped_existing"] == 1


def test_resolve_once_handles_add_failure(monkeypatch):
    issues = [_issue(1, "task A", "body A")]
    monkeypatch.setattr(resolver, "list_open_bot_issues", lambda *a, **k: issues)
    class _FakeStore:
        @staticmethod
        def add(**kw):
            raise RuntimeError("db down")
    _patch_store(monkeypatch, _FakeStore)
    result = resolver.resolve_once(repo="o/r")
    assert result["ok"]
    assert result["created"] == 0
