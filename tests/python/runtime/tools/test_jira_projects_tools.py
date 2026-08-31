"""Jira discovery: projects, boards, sprints, dashboards, remote links, me.

These are the tools an agent uses to find its way around an instance it was
not told the shape of — which is why the loose project-name resolver exists,
and why remote links parse the Confluence page id out of a wiki URL: that id
is the cross-reference a dossier follows from a ticket into its spec page.

Server/DC and Cloud differ here (dashboard create is Cloud-only), so a failure
carries a hint rather than looking like a bug.
"""
from __future__ import annotations

import pytest

from aiforge_core.runtime.tools.jira import _projects as jp


@pytest.fixture()
def rest(monkeypatch):
    state: dict = {"calls": [], "replies": {}, "default": {"ok": True, "data": {}}}

    def _request(method, path, params=None, body=None, **kw):
        state["calls"].append({"method": method, "path": path,
                               "params": params or {}, "body": body or {}})
        for frag, rep in state["replies"].items():
            if frag in path:
                return rep(state) if callable(rep) else rep
        return state["default"]
    monkeypatch.setattr(jp, "_request", _request)
    monkeypatch.setattr(jp, "default_project", lambda: "")
    return state


# ─── remote links ──────────────────────────────────────────────────────


def test_a_confluence_page_id_is_parsed_out_of_the_link(rest):
    """That id is how a dossier follows a ticket into its spec page."""
    rest["replies"]["/remotelink"] = {"ok": True, "data": [
        {"object": {"url": "https://wiki/pages/123456", "title": "Spec"}},
        {"object": {"url": "https://wiki/x?pageId=987654"}},
        {"object": {"url": "https://example.com/other", "title": "Ref"}},
        "not a dict",
    ]}
    out = jp.jira_remote_links({"key": "ENG-1"})
    assert out["count"] == 3
    assert out["links"][0] == {"title": "Spec",
                               "url": "https://wiki/pages/123456",
                               "confluence_page_id": "123456"}
    assert out["links"][1]["confluence_page_id"] == "987654"
    assert out["links"][1]["title"] == "https://wiki/x?pageId=987654"
    assert out["links"][2]["confluence_page_id"] is None


def test_remote_links_need_a_key():
    assert jp.jira_remote_links({})["error"] == "missing 'key'"


def test_a_failed_remote_link_read_is_returned(rest):
    rest["replies"]["/remotelink"] = {"ok": False, "error": "http 404"}
    assert jp.jira_remote_links({"key": "ENG-1"})["ok"] is False


# ─── the authenticated user ────────────────────────────────────────────


def test_the_current_user_is_reported(rest):
    rest["replies"]["/myself"] = {"ok": True, "data": {
        "displayName": "Ada", "accountId": "a1", "emailAddress": "a@b.c",
        "active": True}}
    assert jp.jira_myself({}) == {"ok": True, "display_name": "Ada",
                                  "account_id": "a1", "email": "a@b.c",
                                  "active": True}


def test_a_server_instance_falls_back_to_name_for_the_account_id(rest):
    rest["replies"]["/myself"] = {"ok": True, "data": {"name": "ada"}}
    assert jp.jira_myself({})["account_id"] == "ada"


def test_a_failed_myself_is_returned(rest):
    rest["replies"]["/myself"] = {"ok": False, "error": "http 401"}
    assert jp.jira_myself({})["ok"] is False


# ─── projects ──────────────────────────────────────────────────────────


def test_projects_are_listed(rest):
    rest["replies"]["/project"] = {"ok": True, "data": [
        {"key": "ENG", "name": "Engineering", "id": "1",
         "lead": {"displayName": "Ada"}}]}
    out = jp.jira_projects({})
    assert out["projects"] == [{"key": "ENG", "name": "Engineering",
                                "id": "1", "lead": "Ada"}]


def test_a_paged_project_response_is_also_understood(rest):
    rest["replies"]["/project"] = {"ok": True, "data": {
        "values": [{"key": "ENG", "name": "Eng", "id": "1"}]}}
    assert jp.jira_projects({})["count"] == 1


def test_a_failed_project_list_is_returned(rest):
    rest["replies"]["/project"] = {"ok": False, "error": "http 403"}
    assert jp.jira_projects({})["ok"] is False


def test_a_loose_project_name_resolves_to_its_key(rest, monkeypatch):
    rest["replies"]["/project"] = {"ok": True, "data": [
        {"key": "ENG", "name": "Engineering"}, {"key": "OPS", "name": "Ops"}]}
    seen: dict = {}

    def _pick(name, cands, value_key=None):
        seen.update(name=name, cands=cands)
        return {"ok": True, "key": "ENG"}
    monkeypatch.setattr("aiforge_core.config.repo_map.fuzzy_pick", _pick)
    assert jp.jira_resolve_project({"name": "enginering"})["key"] == "ENG"
    assert seen["cands"]["Engineering"] == "ENG"    # both name and key match
    assert seen["cands"]["ENG"] == "ENG"


def test_resolving_needs_a_name():
    assert jp.jira_resolve_project({})["error"] == "missing 'name'"


def test_an_unreachable_instance_cannot_resolve(rest):
    rest["replies"]["/project"] = {"ok": False, "error": "http 500"}
    assert jp.jira_resolve_project({"name": "eng"})["ok"] is False


# ─── boards + sprints ──────────────────────────────────────────────────


def test_boards_are_listed_and_scoped_to_the_default_project(rest, monkeypatch):
    monkeypatch.setattr(jp, "default_project", lambda: "ENG")
    rest["replies"]["/board"] = {"ok": True, "data": {"values": [
        {"id": 1, "name": "ENG board", "type": "scrum"}, "junk"]}}
    out = jp.jira_boards({})
    assert out["boards"] == [{"id": 1, "name": "ENG board", "type": "scrum"}]
    assert rest["calls"][0]["params"]["projectKeyOrId"] == "ENG"


def test_an_explicit_project_filter_wins(rest, monkeypatch):
    monkeypatch.setattr(jp, "default_project", lambda: "ENG")
    rest["replies"]["/board"] = {"ok": True, "data": {"values": []}}
    jp.jira_boards({"project": "OPS"})
    assert rest["calls"][0]["params"]["projectKeyOrId"] == "OPS"


def test_a_failed_board_list_is_returned(rest):
    rest["replies"]["/board"] = {"ok": False, "error": "no agile api"}
    assert jp.jira_boards({})["ok"] is False


def test_sprints_are_listed_for_a_board(rest):
    rest["replies"]["/sprint"] = {"ok": True, "data": {"values": [
        {"id": 9, "name": "Sprint 9", "state": "active",
         "startDate": "2026-01-01", "endDate": "2026-01-14"}]}}
    out = jp.jira_sprints({"board_id": "1", "state": "active"})
    assert out["sprints"][0]["start"] == "2026-01-01"
    assert rest["calls"][0]["params"]["state"] == "active"


def test_sprints_need_a_board(rest):
    assert jp.jira_sprints({})["error"] == "missing 'board_id'"


def test_a_failed_sprint_list_is_returned(rest):
    rest["replies"]["/sprint"] = {"ok": False, "error": "http 404"}
    assert jp.jira_sprints({"board": "1"})["ok"] is False


def test_sprint_issues_are_summarised(rest, monkeypatch):
    monkeypatch.setattr(jp, "_issue_summary",
                        lambda x, with_time=False: (x["key"], with_time))
    rest["replies"]["/issue"] = {"ok": True, "data": {
        "issues": [{"key": "ENG-1"}], "total": 1}}
    out = jp.jira_sprint_issues({"sprint_id": "9"})
    assert out["results"] == [("ENG-1", False)] and out["total"] == 1


def test_sprint_issue_time_tracking_is_opt_in(rest, monkeypatch):
    monkeypatch.setattr(jp, "_issue_summary", lambda x, with_time=False: with_time)
    rest["replies"]["/issue"] = {"ok": True, "data": {"issues": [{"key": "A"}]}}
    jp.jira_sprint_issues({"sprint_id": "9", "time": "true"})
    assert jp._TIME_FIELDS in rest["calls"][0]["params"]["fields"]


def test_sprint_issues_need_a_sprint(rest):
    assert jp.jira_sprint_issues({})["error"] == "missing 'sprint_id'"


def test_a_failed_sprint_issue_read_is_returned(rest):
    rest["replies"]["/issue"] = {"ok": False, "error": "http 403"}
    assert jp.jira_sprint_issues({"sprint": "9"})["ok"] is False


# ─── dashboards ────────────────────────────────────────────────────────


def test_dashboards_are_listed(rest):
    rest["replies"]["/dashboard"] = {"ok": True, "data": {"dashboards": [
        {"id": "1", "name": "Ops", "view": "https://j/d/1"}]}}
    assert jp.jira_dashboards({})["dashboards"] == [
        {"id": "1", "name": "Ops", "url": "https://j/d/1"}]


def test_a_values_shaped_dashboard_response_also_works(rest):
    rest["replies"]["/dashboard"] = {"ok": True, "data": {"values": [
        {"id": "2", "name": "Eng", "self": "https://j/rest/2"}]}}
    assert jp.jira_dashboards({})["dashboards"][0]["url"] == "https://j/rest/2"


def test_a_failed_dashboard_list_is_returned(rest):
    rest["replies"]["/dashboard"] = {"ok": False, "error": "http 401"}
    assert jp.jira_dashboards({})["ok"] is False


def test_one_dashboard_is_read_with_its_gadgets(rest):
    calls = {"n": 0}

    def _reply(_state):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"ok": True, "data": {"id": "1", "name": "Ops",
                                         "view": "https://j/d/1"}}
        return {"ok": True, "data": {"gadgets": [{"id": "g1", "title": "Filter"},
                                                 "junk"]}}
    rest["replies"]["/dashboard"] = _reply
    out = jp.jira_dashboard_read({"id": "1"})
    assert out["name"] == "Ops"
    assert out["gadgets"] == [{"title": "Filter", "id": "g1"}]


def test_a_dashboard_whose_gadgets_fail_still_reads(rest):
    calls = {"n": 0}

    def _reply(_state):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"ok": True, "data": {"id": "1", "name": "Ops"}}
        return {"ok": False, "error": "http 403"}
    rest["replies"]["/dashboard"] = _reply
    assert jp.jira_dashboard_read({"dashboard_id": "1"})["gadgets"] == []


def test_reading_a_dashboard_needs_an_id():
    assert jp.jira_dashboard_read({})["error"] == "missing 'id'"


def test_a_failed_dashboard_read_is_returned(rest):
    rest["replies"]["/dashboard"] = {"ok": False, "error": "http 404"}
    assert jp.jira_dashboard_read({"id": "9"})["ok"] is False


@pytest.mark.parametrize("share,perm", [
    ("private", []),
    ("authenticated", [{"type": "authenticated"}]),
    ("global", [{"type": "global"}]),
    ("typo", []),
])
def test_a_dashboard_is_created_with_its_share_permissions(rest, share, perm):
    rest["replies"]["/dashboard"] = {"ok": True, "data": {"id": "5",
                                                          "name": "Ops"}}
    jp.jira_dashboard_create({"name": "Ops", "share": share})
    assert rest["calls"][0]["body"]["sharePermissions"] == perm


def test_creating_a_dashboard_needs_a_name():
    assert jp.jira_dashboard_create({})["error"] == "missing 'name'"


def test_a_server_instance_gets_a_hint_rather_than_a_bare_failure(rest):
    """Dashboard create is Cloud-only; on Server/DC it is a UI action."""
    rest["replies"]["/dashboard"] = {"ok": False, "error": "http 404"}
    out = jp.jira_dashboard_create({"name": "Ops"})
    assert out["ok"] is False and "Jira Cloud" in out["hint"]


def test_an_existing_hint_is_not_overwritten(rest):
    rest["replies"]["/dashboard"] = {"ok": False, "error": "http 400",
                                     "hint": "name already taken"}
    assert jp.jira_dashboard_create({"name": "Ops"})["hint"] == "name already taken"
