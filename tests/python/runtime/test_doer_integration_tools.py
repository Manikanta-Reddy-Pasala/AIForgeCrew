"""The pipeline Doer's integration tools.

Every function here is a typed wrapper the ADK FunctionTool layer can
advertise, delegating to the same REST client the chat agent uses. The wrapper
is where a name can drift: a payload key the client does not read, or an
argument silently dropped, turns into an empty result that looks like "no
data" rather than an error. So the tests call each wrapper with distinctive
values and assert what actually reaches the client.

Nothing here touches the network — every client function is stubbed.
"""
from __future__ import annotations

import pytest

from aiforge_core.runtime.doer_tools import _integrations as it


@pytest.fixture
def calls(monkeypatch, tmp_path):
    """Capture (payload, cwd) for every stubbed client function."""
    from aiforge_core.runtime import sandbox
    monkeypatch.setattr(sandbox, "root", lambda: tmp_path)
    monkeypatch.setattr(it, "root", lambda: tmp_path)
    seen: dict = {}

    def _stub(mod, name):
        def _fn(payload, cwd=None):
            seen["name"] = name
            seen["payload"] = payload
            seen["cwd"] = cwd
            return {"ok": True}
        monkeypatch.setattr(mod, name, _fn, raising=False)

    from aiforge_core.runtime.tools import confluence, email_tool, gitlab, jira
    for mod in (confluence, jira, gitlab, email_tool):
        for name in dir(mod):
            fn = getattr(mod, name)
            if callable(fn) and not name.startswith("__"):
                _stub(mod, name)
    seen["_root"] = str(tmp_path)
    return seen


# Each row: wrapper name, kwargs to call it with, payload keys that must arrive.
_WRAPPERS = [
    ("confluence_search", {"query": "q", "cql": "c", "limit": 3},
     {"query": "q", "cql": "c", "limit": 3}),
    ("confluence_read", {"id": "1", "title": "t", "space": "S"},
     {"id": "1", "title": "t", "space": "S"}),
    ("confluence_create", {"title": "t", "space": "S", "body": "b", "parent_id": "9"},
     {"title": "t", "space": "S", "body": "b", "parent_id": "9"}),
    ("confluence_update", {"id": "1", "body": "b", "title": "t"},
     {"id": "1", "body": "b", "title": "t"}),
    ("confluence_children", {"id": "1", "limit": 7}, {"id": "1", "limit": 7}),
    ("confluence_attach", {"id": "1", "path": "/tmp/a.png"},
     {"id": "1", "path": "/tmp/a.png"}),
    ("confluence_spaces", {"limit": 5}, {"limit": 5}),
    ("confluence_page_by_title", {"space": "S", "title": "t"},
     {"space": "S", "title": "t"}),
    ("confluence_labels", {"id": "1"}, {"id": "1"}),
    ("confluence_add_label", {"id": "1", "labels": "a,b"}, {"id": "1", "labels": "a,b"}),
    ("confluence_comments", {"id": "1", "limit": 4}, {"id": "1", "limit": 4}),
    ("confluence_comment", {"id": "1", "body": "hi"}, {"id": "1", "body": "hi"}),
    ("confluence_descendants", {"id": "1", "limit": 8}, {"id": "1", "limit": 8}),
    ("confluence_resolve_space", {"name": "Eng"}, {"name": "Eng"}),
    ("jira_search", {"query": "q", "jql": "j", "limit": 10},
     {"query": "q", "jql": "j", "limit": 10}),
    ("jira_read", {"key": "ENG-1"}, {"key": "ENG-1"}),
    ("jira_worklog", {"key": "ENG-1", "limit": 5}, {"key": "ENG-1", "limit": 5}),
    ("jira_remote_links", {"key": "ENG-1"}, {"key": "ENG-1"}),
    ("jira_resolve_project", {"name": "eng"}, {"name": "eng"}),
    ("jira_log_work", {"key": "ENG-1", "time_spent": "2h", "comment": "c"},
     {"key": "ENG-1", "time_spent": "2h", "comment": "c"}),
    ("jira_myself", {}, {}),
    ("jira_projects", {"limit": 5}, {"limit": 5}),
    ("jira_boards", {"project": "ENG", "limit": 5}, {"project": "ENG", "limit": 5}),
    ("jira_sprints", {"board_id": "7", "state": "active", "limit": 5},
     {"board_id": "7", "state": "active", "limit": 5}),
    ("jira_dashboards", {"limit": 5}, {"limit": 5}),
    ("jira_dashboard_read", {"id": "3"}, {"id": "3"}),
    ("jira_comment", {"key": "ENG-1", "body": "hi"}, {"key": "ENG-1", "body": "hi"}),
    ("jira_transitions", {"key": "ENG-1"}, {"key": "ENG-1"}),
    ("jira_transition", {"key": "ENG-1", "transition": "Done", "comment": "c"},
     {"key": "ENG-1", "transition": "Done", "comment": "c"}),
    ("jira_assign", {"key": "ENG-1", "assignee": "me"},
     {"key": "ENG-1", "assignee": "me"}),
    ("gitlab_read", {"project": "grp/p", "iid": "3"}, {"project": "grp/p", "iid": "3"}),
    ("gitlab_comment", {"project": "grp/p", "iid": "3", "body": "hi"},
     {"project": "grp/p", "iid": "3", "body": "hi"}),
    ("gitlab_mr_comment", {"project": "grp/p", "iid": "3", "body": "hi"},
     {"project": "grp/p", "iid": "3", "body": "hi"}),
]


@pytest.mark.parametrize("fn_name,kwargs,expected", _WRAPPERS,
                         ids=[w[0] for w in _WRAPPERS])
def test_every_argument_reaches_the_client(calls, fn_name, kwargs, expected):
    out = getattr(it, fn_name)(**kwargs)
    assert out == {"ok": True}
    for key, value in expected.items():
        assert calls["payload"][key] == value, f"{fn_name}: {key} was dropped"
    assert calls["cwd"] == calls["_root"], f"{fn_name}: wrong repo root"


def test_the_client_is_called_under_the_current_repo_root(calls, tmp_path):
    """Every wrapper passes the sandbox root, so a chat scoped to another repo
    reaches that repo's credentials/config, not the process default."""
    it.jira_read(key="ENG-1")
    assert calls["cwd"] == str(tmp_path)


# ─── wrappers with a different shape ───────────────────────────────────


def test_sprint_issues_carries_the_time_flag(calls):
    it.jira_sprint_issues(sprint_id="9", time=True, limit=5)
    assert calls["payload"] == {"sprint_id": "9", "time": True, "limit": 5}


def test_creating_an_issue(calls):
    it.jira_create(project="ENG", summary="s", description="d",
                   issuetype="Bug", labels="a,b")
    assert calls["payload"] == {"project": "ENG", "summary": "s",
                                "description": "d", "issuetype": "Bug",
                                "labels": "a,b"}


def test_an_update_sends_only_the_fields_that_were_given(calls):
    """An empty string means "not supplied" — sending it would blank the field
    in Jira."""
    it.jira_update(key="ENG-1", summary="s")
    assert calls["payload"] == {"key": "ENG-1", "summary": "s"}


def test_a_status_change_rides_the_same_update(calls):
    it.jira_update(key="ENG-1", status="Done", labels="a")
    assert calls["payload"] == {"key": "ENG-1", "labels": "a", "status": "Done"}


def test_linking_two_issues(calls):
    it.jira_link_issues(inward="ENG-1", outward="ENG-2", type="Blocks")
    p = calls["payload"]
    assert p["inward"] == "ENG-1"
    assert p["outward"] == "ENG-2"
    assert p["type"] == "Blocks"


def test_creating_a_dashboard(calls):
    it.jira_dashboard_create(name="Board", description="d")
    assert calls["payload"]["name"] == "Board"


def test_searching_gitlab(calls):
    it.gitlab_search(query="q", limit=5, state="opened")
    p = calls["payload"]
    assert p["query"] == "q"
    assert p["limit"] == 5
    assert p["state"] == "opened"


def test_creating_a_gitlab_issue(calls):
    it.gitlab_create(project="grp/p", title="t", description="d")
    assert calls["payload"]["title"] == "t"


def test_updating_a_gitlab_issue(calls):
    it.gitlab_update(project="grp/p", iid="3", title="t")
    assert calls["payload"]["iid"] == "3"


def test_opening_a_merge_request(calls):
    it.gitlab_mr_create(project="grp/p", source_branch="feat", title="t")
    p = calls["payload"]
    assert p["source_branch"] == "feat"
    assert p["title"] == "t"


def test_listing_pipelines(calls):
    it.gitlab_pipelines(project="grp/p", ref="main", status="failed", limit=5)
    p = calls["payload"]
    assert p["ref"] == "main"
    assert p["status"] == "failed"


def test_reading_one_pipeline(calls):
    it.gitlab_pipeline(project="grp/p", pipeline_id=42)
    assert calls["payload"]["pipeline_id"] == 42


def test_watching_a_pipeline(calls):
    it.gitlab_pipeline_watch(project="grp/p", pipeline_id=42)
    assert calls["payload"]["pipeline_id"] == 42


def test_sending_email(calls):
    it.email_send(to="a@b.c", subject="s", body="b")
    p = calls["payload"]
    assert p["to"] == "a@b.c"
    assert p["subject"] == "s"


def test_reading_email(calls):
    it.email_read(folder="INBOX", limit=3, query="q")
    p = calls["payload"]
    assert p["folder"] == "INBOX"
    assert p["limit"] == 3


# ─── the two non-REST wrappers ─────────────────────────────────────────


def test_a_repo_name_is_resolved_through_the_repo_map(monkeypatch):
    from aiforge_core.config import repo_map
    seen: dict = {}
    def _resolve(name):
        seen["name"] = name
        return {"path": "/repo"}
    monkeypatch.setattr(repo_map, "resolve", _resolve)
    assert it.resolve_repo("aiforge crew") == {"path": "/repo"}
    assert seen["name"] == "aiforge crew"


def test_resolve_repo_is_defined_twice_and_the_second_wins(monkeypatch):
    """Two same-named defs sit in this module; the LAST one is what callers
    get. They differ: the shadowed one coerces a None name to "", the live one
    passes it straight through. Pinned so the duplicate cannot quietly change
    which behaviour ships."""
    from aiforge_core.config import repo_map
    seen: dict = {}
    def _resolve(name):
        seen["name"] = name
        return {}
    monkeypatch.setattr(repo_map, "resolve", _resolve)
    it.resolve_repo(None)
    assert seen["name"] is None


def test_a_dossier_is_gathered_for_the_doer_role(monkeypatch):
    from aiforge_core.runtime import context_gather as cg
    seen: dict = {}

    def _gather(kind, key, force=False, role=None):
        seen.update(kind=kind, key=key, force=force, role=role)
        return {"ok": True}
    monkeypatch.setattr(cg, "gather", _gather)
    assert it.context_gather("jira", "ENG-1", force=True) == {"ok": True}
    assert seen == {"kind": "jira", "key": "ENG-1", "force": True, "role": "doer"}
