from __future__ import annotations

import pytest

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
        def get(ident):
            return None

        @staticmethod
        def create(*, project, title, body, metadata, identifier):
            added.append({"project": project, "title": title, "body": body,
                          "metadata": metadata, "identifier": identifier})

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
        def create(**kw):
            raise AssertionError("should not be called when existing found")

        @staticmethod
        def get(ident):
            return {"id": 99} if ident == resolver._identifier_for(
                "https://github.com/o/r/issues/1") else None
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
        def get(ident):
            return None

        @staticmethod
        def create(**kw):
            raise RuntimeError("db down")
    _patch_store(monkeypatch, _FakeStore)
    result = resolver.resolve_once(repo="o/r")
    assert result["ok"]
    assert result["created"] == 0


def test_the_identifier_is_stable_per_issue():
    url = "https://github.com/octo-org/My.Repo/issues/12"
    ident = resolver._identifier_for(url)
    assert ident == resolver._identifier_for(url)
    assert ident.startswith("GH-octo-org-my-repo-12-")


def test_look_alike_repos_get_different_identifiers():
    a = resolver._identifier_for("https://github.com/a-b/c/issues/1")
    b = resolver._identifier_for("https://github.com/a/b-c/issues/1")
    c = resolver._identifier_for("https://ghe.example.com/a-b/c/issues/1")
    assert len({a, b, c}) == 3


def test_an_issue_without_a_url_is_not_imported(monkeypatch):
    issue = _issue(3, "no url", "b")
    issue["html_url"] = ""
    monkeypatch.setattr(resolver, "list_open_bot_issues", lambda *a, **k: [issue])

    class _FakeStore:
        @staticmethod
        def get(ident):
            raise AssertionError("no lookup without a url")

        @staticmethod
        def create(**kw):
            raise AssertionError("no import without a url")
    _patch_store(monkeypatch, _FakeStore)
    assert resolver.resolve_once(repo="o/r")["created"] == 0


@pytest.fixture
def real_store(monkeypatch, tmp_path):
    """Hermetic embedded-SQLite store (same recipe as tickets/test_lease)."""
    import importlib
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_TICKETS_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_DB_PATH", str(tmp_path / "t.db"))
    import aiforge_core.config.env as envmod
    importlib.reload(envmod)
    from aiforge_core.tickets import backend_factory
    importlib.reload(backend_factory)
    backend_factory.reset_backend_for_tests()
    from aiforge_core.tickets import store
    importlib.reload(store)
    yield store
    backend_factory.reset_backend_for_tests()


def test_a_real_store_imports_an_issue_once(monkeypatch, real_store):
    """Against the real ticket store: the second poll finds the first import,
    even after that ticket is done."""
    store = real_store
    issues = [_issue(7, "task G", "body G")]
    monkeypatch.setattr(resolver, "list_open_bot_issues", lambda *a, **k: issues)
    first = resolver.resolve_once(repo="o/r", project="X")
    assert first["created"] == 1
    ticket = store.get(resolver._identifier_for("https://github.com/o/r/issues/7"))
    assert ticket is not None and ticket.title == "task G"
    store.update_status(ticket.id, "done")
    second = resolver.resolve_once(repo="o/r", project="X")
    assert second["created"] == 0 and second["skipped_existing"] == 1
