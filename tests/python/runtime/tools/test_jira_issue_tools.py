"""The Jira issue tools: search, read, worklog, create, update, workflow.

Every function here returns ``{"ok": bool, ...}`` and never raises into the
agent loop, so the tests drive the REST seam (``jira._request``) directly and
check both halves of that contract.

Two behaviours carry real history. A bare full-text search is SCOPED to the
default project — without it a job's filter silently searched every project
the token can see. And Jira's status is not an editable field: it moves only
through a workflow transition, so a ``status`` argument to jira_update is
routed to jira_transition rather than PUT into fields where it is ignored.
"""
from __future__ import annotations

import pytest

from aiforge_core.runtime.tools import jira


@pytest.fixture()
def rest(monkeypatch):
    """Capture Jira REST calls; queue replies per (method, path-fragment)."""
    state: dict = {"calls": [], "replies": {}, "default": {"ok": True, "data": {}}}

    def _request(method, path, params=None, body=None, **kw):
        state["calls"].append({"method": method, "path": path,
                               "params": params or {}, "body": body or {}})
        for frag, rep in state["replies"].items():
            if frag in path:
                return rep(state) if callable(rep) else rep
        return state["default"]
    monkeypatch.setattr(jira, "_request", _request)
    monkeypatch.setattr(jira, "default_project", lambda: "")
    monkeypatch.setattr(jira, "_issue_url", lambda key: f"https://j/browse/{key}")
    return state


def _issues(*keys, total=None):
    return {"ok": True, "data": {"issues": [{"key": k, "fields": {"summary": k}}
                                            for k in keys],
                                 "total": total if total is not None else len(keys)}}


# ─── search scoping ────────────────────────────────────────────────────


def test_a_full_text_query_becomes_jql(monkeypatch):
    monkeypatch.setattr(jira, "default_project", lambda: "")
    jql, err = jira._search_jql({"query": 'say "hi"'})
    assert err is None
    assert jql == 'text ~ "say \\"hi\\"" ORDER BY updated DESC'


def test_a_bare_query_is_scoped_to_the_default_project(monkeypatch):
    """Unscoped, a job's filter searches every project the token can see."""
    monkeypatch.setattr(jira, "default_project", lambda: "ENG")
    jql, _ = jira._search_jql({"query": "crash"})
    assert jql.startswith('project = "ENG" AND (text ~ "crash"')
    assert jql.rstrip().endswith("ORDER BY updated DESC")


def test_the_order_by_clause_stays_outside_the_scope_parens(monkeypatch):
    monkeypatch.setattr(jira, "default_project", lambda: "ENG")
    jql, _ = jira._search_jql({"jql": "status = Open ORDER BY created"})
    assert jql == 'project = "ENG" AND (status = Open) ORDER BY created'


def test_an_explicit_project_in_the_jql_wins(monkeypatch):
    monkeypatch.setattr(jira, "default_project", lambda: "ENG")
    jql, _ = jira._search_jql({"jql": 'project = "OPS" AND status = Open'})
    assert jql == 'project = "OPS" AND status = Open'


def test_an_explicit_project_argument_is_used(monkeypatch):
    monkeypatch.setattr(jira, "default_project", lambda: "ENG")
    jql, _ = jira._search_jql({"query": "crash", "project": "OPS"})
    assert jql.startswith('project = "OPS"')


def test_a_search_with_neither_query_nor_jql_is_an_error():
    _jql, err = jira._search_jql({})
    assert err == {"ok": False, "error": "missing 'query' or 'jql'"}


@pytest.mark.parametrize("raw,expected", [
    (10, 10), ("all", 500), (0, 500), (-1, 500), ("", 500),
    (9999, 500), ("junk", 50), (None, 50),
])
def test_the_search_limit_and_its_safety_cap(monkeypatch, raw, expected):
    monkeypatch.setattr(jira, "_search_cap", lambda: 500)
    assert jira._search_limit({"limit": raw}) == expected


@pytest.mark.parametrize("args,start", [
    ({"startAt": 20}, 20), ({"start_at": 5}, 5), ({"startAt": -3}, 0),
    ({"startAt": "junk"}, 0), ({}, 0),
])
def test_the_search_offset(args, start):
    assert jira._search_start(args) == start


# ─── search paging ─────────────────────────────────────────────────────


def test_a_single_page_search(rest, monkeypatch):
    monkeypatch.setattr(jira, "_issue_summary", lambda x, with_time=False: x["key"])
    rest["replies"]["/search"] = _issues("ENG-1", "ENG-2")
    out = jira.jira_search({"jql": "status = Open"})
    assert out == {"ok": True, "results": ["ENG-1", "ENG-2"], "total": 2,
                   "count": 2, "truncated": False}


def test_paging_continues_until_the_limit(rest, monkeypatch):
    monkeypatch.setattr(jira, "_issue_summary", lambda x, with_time=False: x["key"])
    monkeypatch.setattr(jira, "_SEARCH_PAGE", 2)
    pages = [_issues("A", "B", total=4), _issues("C", "D", total=4)]
    rest["replies"]["/search"] = lambda st: pages.pop(0)
    out = jira.jira_search({"jql": "x", "limit": 4})
    assert out["results"] == ["A", "B", "C", "D"] and out["truncated"] is False


def test_more_matches_than_asked_for_marks_truncated(rest, monkeypatch):
    monkeypatch.setattr(jira, "_issue_summary", lambda x, with_time=False: x["key"])
    rest["replies"]["/search"] = _issues("A", total=50)
    out = jira.jira_search({"jql": "x", "limit": 1})
    assert out["truncated"] is True and out["total"] == 50


def test_a_short_page_ends_the_walk(rest, monkeypatch):
    monkeypatch.setattr(jira, "_issue_summary", lambda x, with_time=False: x["key"])
    rest["replies"]["/search"] = _issues(total=10)          # empty page
    assert jira.jira_search({"jql": "x", "limit": 5})["count"] == 0


def test_a_first_page_failure_is_returned_as_is(rest):
    rest["replies"]["/search"] = {"ok": False, "error": "http 401"}
    assert jira.jira_search({"jql": "x"}) == {"ok": False, "error": "http 401"}


def test_a_later_page_failure_keeps_what_was_fetched(rest, monkeypatch):
    monkeypatch.setattr(jira, "_issue_summary", lambda x, with_time=False: x["key"])
    monkeypatch.setattr(jira, "_SEARCH_PAGE", 1)
    pages = [_issues("A", total=9), {"ok": False, "error": "http 500"}]
    rest["replies"]["/search"] = lambda st: pages.pop(0)
    out = jira.jira_search({"jql": "x", "limit": 5})
    assert out["ok"] is True and out["results"] == ["A"]
    assert out["truncated"] is True and out["error"] == "http 500"


def test_time_tracking_fields_are_opt_in(rest, monkeypatch):
    monkeypatch.setattr(jira, "_issue_summary", lambda x, with_time=False: x["key"])
    rest["replies"]["/search"] = _issues("A")
    jira.jira_search({"jql": "x"})
    assert jira._TIME_FIELDS not in rest["calls"][0]["params"]["fields"]
    rest["calls"].clear()
    jira.jira_search({"jql": "x", "time": "true"})
    assert jira._TIME_FIELDS in rest["calls"][0]["params"]["fields"]


# ─── reading an issue ──────────────────────────────────────────────────


def test_a_key_is_required():
    for fn in (jira.jira_read, jira.jira_worklog, jira.jira_comments,
               jira.jira_comment, jira.jira_transitions, jira.jira_transition,
               jira.jira_assign, jira.jira_update):
        assert fn({})["error"] == "missing 'key'"


def test_an_issue_view_flattens_the_named_objects():
    view = jira._issue_view(
        {"key": "ENG-1"},
        {"summary": "s", "issuetype": {"name": "Bug"},
         "status": {"name": "Open"}, "assignee": {"displayName": "Ada"},
         "reporter": None, "priority": {"name": "High"}, "labels": ["a"],
         "description": "d",
         "comment": {"comments": [{"author": {"displayName": "Bo"},
                                   "body": "hi"}]}})
    assert view["type"] == "Bug" and view["status"] == "Open"
    assert view["assignee"] == "Ada" and view["reporter"] is None
    assert view["comments"] == [{"author": "Bo", "body": "hi"}]


def test_a_read_pulls_attachments_by_default(rest, monkeypatch):
    rest["replies"]["/issue/"] = {"ok": True, "data": {
        "key": "ENG-1", "fields": {"summary": "s",
                                   "attachment": [{"filename": "a.png"}]}}}
    monkeypatch.setattr(jira, "_fetch_attachments",
                        lambda atts, save_ctx=None: [{"filename": "a.png"}])
    out = jira.jira_read({"key": "ENG-1"})
    assert out["attachments"] == [{"filename": "a.png"}]


def test_attachments_can_be_turned_off(rest, monkeypatch):
    rest["replies"]["/issue/"] = {"ok": True, "data": {"key": "ENG-1",
                                                       "fields": {}}}
    monkeypatch.setattr(jira, "_fetch_attachments",
                        lambda *a, **k: pytest.fail("fetched with attachments=false"))
    assert "attachments" not in jira.jira_read({"key": "ENG-1",
                                                "attachments": "false"})


def test_a_failed_read_is_returned_as_is(rest):
    rest["replies"]["/issue/"] = {"ok": False, "error": "http 404"}
    assert jira.jira_read({"key": "ENG-9"})["error"] == "http 404"


# ─── worklogs ──────────────────────────────────────────────────────────


def test_worklog_rows_sum_the_seconds():
    rows, total = jira._worklog_rows([
        {"author": {"displayName": "Ada"}, "timeSpent": "2h",
         "timeSpentSeconds": 7200, "started": "2026-01-01", "comment": "c"},
        {"author": None, "timeSpentSeconds": "junk"},
    ])
    assert total == 7200 and rows[0]["author"] == "Ada"
    assert rows[1]["author"] is None


def test_a_worklog_page_reports_truncation(rest, monkeypatch):
    monkeypatch.setattr(jira, "_time_rollup", lambda key: {"time_spent": "5h"})
    monkeypatch.setattr(jira, "_fmt_secs", lambda s: f"{s}s")
    rest["replies"]["/worklog"] = {"ok": True, "data": {
        "worklogs": [{"timeSpentSeconds": 3600}], "total": 12}}
    out = jira.jira_worklog({"key": "ENG-1"})
    assert out["worklog_count"] == 1 and out["truncated"] is True
    assert out["tracking"] == {"time_spent": "5h"}


def test_a_failed_worklog_read_is_returned(rest):
    rest["replies"]["/worklog"] = {"ok": False, "error": "http 403"}
    assert jira.jira_worklog({"key": "ENG-1"})["error"] == "http 403"


def test_the_time_rollup_is_a_separate_cheap_call(rest, monkeypatch):
    monkeypatch.setattr(jira, "_time_fields", lambda f: {"time_spent": "1h"})
    rest["replies"]["/issue/"] = {"ok": True, "data": {"fields": {}}}
    assert jira._time_rollup("ENG-1") == {"time_spent": "1h"}


def test_a_failed_rollup_is_none(rest):
    rest["replies"]["/issue/"] = {"ok": False, "error": "http 500"}
    assert jira._time_rollup("ENG-1") is None


def test_logging_work_needs_a_key_and_a_duration():
    assert "time_spent" in jira.jira_log_work({"key": "ENG-1"})["error"]
    assert "time_spent" in jira.jira_log_work({"time_spent": "2h"})["error"]


def test_work_is_logged_with_a_converted_comment(rest, monkeypatch):
    monkeypatch.setattr(jira, "to_jira_wiki", lambda s: f"wiki:{s}")
    out = jira.jira_log_work({"key": "ENG-1", "time_spent": "2h",
                              "comment": "**done**", "started": "2026-01-01"})
    assert out == {"ok": True, "key": "ENG-1", "logged": "2h",
                   "url": "https://j/browse/ENG-1"}
    body = rest["calls"][0]["body"]
    assert body == {"timeSpent": "2h", "comment": "wiki:**done**",
                    "started": "2026-01-01"}


# ─── creating ──────────────────────────────────────────────────────────


def test_a_create_needs_a_project_and_summary(rest):
    assert jira.jira_create({"summary": "s"})["error"] == "missing 'project'"
    assert jira.jira_create({"project": "ENG"})["error"] == "missing 'summary'"


def test_the_default_project_fills_a_missing_one(rest, monkeypatch):
    monkeypatch.setattr(jira, "default_project", lambda: "ENG")
    rest["replies"]["/issue"] = {"ok": True, "data": {"key": "ENG-7"}}
    assert jira.jira_create({"summary": "s"})["key"] == "ENG-7"
    assert rest["calls"][0]["body"]["fields"]["project"] == {"key": "ENG"}


def test_every_optional_field_reaches_the_payload(rest, monkeypatch):
    monkeypatch.setattr(jira, "to_jira_wiki", lambda s: f"wiki:{s}")
    rest["replies"]["/issue"] = {"ok": True, "data": {"key": "ENG-7"}}
    jira.jira_create({"project": "ENG", "summary": "s", "description": "d",
                      "priority": "High", "assignee": "ada",
                      "labels": "a, b", "parent": "ENG-1",
                      "issuetype": "Bug"})
    fields = rest["calls"][0]["body"]["fields"]
    assert fields["issuetype"] == {"name": "Bug"}
    assert fields["description"] == "wiki:d"
    assert fields["labels"] == ["a", "b"]              # comma string split
    assert fields["parent"] == {"key": "ENG-1"}
    assert fields["assignee"] == {"name": "ada"}


def test_a_create_failure_is_returned(rest):
    rest["replies"]["/issue"] = {"ok": False, "error": "http 400"}
    assert jira.jira_create({"project": "E", "summary": "s"})["ok"] is False


# ─── updating ──────────────────────────────────────────────────────────


def test_a_status_argument_is_routed_to_a_transition(rest, monkeypatch):
    """Jira status is not an editable field — a PUT would be ignored."""
    seen: dict = {}

    def _transition(args, cwd=None):
        seen.update(args)
        return {"ok": True}
    monkeypatch.setattr(jira, "jira_transition", _transition)
    out = jira.jira_update({"key": "ENG-1", "status": "In Progress"})
    assert seen["transition"] == "In Progress"
    assert out == {"ok": True, "key": "ENG-1", "status": "In Progress",
                   "transitioned": True, "url": "https://j/browse/ENG-1"}


def test_a_status_inside_raw_fields_is_also_routed(monkeypatch):
    raw = {"status": {"name": "Done"}, "customfield_1": "x"}
    assert jira._wanted_status({}, raw) == "Done"
    assert "status" not in raw                       # popped out of the PUT


def test_no_status_anywhere(monkeypatch):
    assert jira._wanted_status({}, {"customfield_1": "x"}) == ""


def test_a_failed_transition_stops_the_update(rest, monkeypatch):
    monkeypatch.setattr(jira, "jira_transition",
                        lambda args, cwd=None: {"ok": False, "error": "no such"})
    assert jira.jira_update({"key": "ENG-1", "status": "Nope",
                             "summary": "s"})["error"] == "no such"


def test_an_update_with_nothing_to_change(rest):
    assert jira.jira_update({"key": "ENG-1"})["error"] == "no fields to update"


def test_raw_fields_merge_last_and_win(rest, monkeypatch):
    monkeypatch.setattr(jira, "to_jira_wiki", lambda s: s)
    jira.jira_update({"key": "ENG-1", "summary": "mine",
                      "fields": {"summary": "theirs"}})
    assert rest["calls"][0]["body"]["fields"]["summary"] == "theirs"


def test_a_description_can_be_cleared(rest, monkeypatch):
    monkeypatch.setattr(jira, "to_jira_wiki", lambda s: s)
    jira.jira_update({"key": "ENG-1", "description": ""})
    assert rest["calls"][0]["body"]["fields"]["description"] == ""


def test_the_written_summary_reports_what_changed(rest, monkeypatch):
    monkeypatch.setattr(jira, "to_jira_wiki", lambda s: s)
    out = jira.jira_update({"key": "ENG-1", "summary": "s", "labels": ["a"]})
    assert out["written"] == {"summary": "s", "labels": ["a"]}
    assert out["transitioned"] is False


def test_a_failed_update_is_returned(rest):
    rest["replies"]["/issue/"] = {"ok": False, "error": "http 400"}
    assert jira.jira_update({"key": "ENG-1", "summary": "s"})["ok"] is False


# ─── comments ──────────────────────────────────────────────────────────


def test_comments_are_read_with_their_authors(rest):
    rest["replies"]["/comment"] = {"ok": True, "data": {
        "comments": [{"id": "1", "author": {"displayName": "Ada"},
                      "created": "2026", "updated": "2026", "body": "hi"}],
        "total": 5}}
    out = jira.jira_comments({"key": "ENG-1"})
    assert out["comments"][0]["author"] == "Ada"
    assert out["count"] == 1 and out["truncated"] is True


@pytest.mark.parametrize("raw,expected", [(10, 10), (999, 100), (0, 1),
                                          ("junk", 50)])
def test_the_comment_page_size_is_clamped(rest, raw, expected):
    rest["replies"]["/comment"] = {"ok": True, "data": {"comments": []}}
    jira.jira_comments({"key": "ENG-1", "limit": raw})
    assert rest["calls"][0]["params"]["maxResults"] == expected


def test_a_failed_comment_read_is_returned(rest):
    rest["replies"]["/comment"] = {"ok": False, "error": "http 403"}
    assert jira.jira_comments({"key": "ENG-1"})["ok"] is False


def test_posting_a_comment_converts_the_body(rest, monkeypatch):
    monkeypatch.setattr(jira, "to_jira_wiki", lambda s: f"wiki:{s}")
    rest["replies"]["/comment"] = {"ok": True, "data": {"id": "42"}}
    out = jira.jira_comment({"key": "ENG-1", "body": "## heading"})
    assert out["id"] == "42"
    assert rest["calls"][0]["body"] == {"body": "wiki:## heading"}


def test_posting_needs_a_body(rest):
    assert jira.jira_comment({"key": "ENG-1"})["error"] == "missing 'body'"


def test_a_failed_post_is_returned(rest):
    rest["replies"]["/comment"] = {"ok": False, "error": "http 401"}
    assert jira.jira_comment({"key": "ENG-1", "body": "x"})["ok"] is False


# ─── workflow ──────────────────────────────────────────────────────────


def test_available_transitions_are_listed(rest):
    rest["replies"]["/transitions"] = {"ok": True, "data": {"transitions": [
        {"id": "11", "name": "Start", "to": {"name": "In Progress"}}]}}
    out = jira.jira_transitions({"key": "ENG-1"})
    assert out["transitions"] == [{"id": "11", "name": "Start",
                                   "to": "In Progress"}]


def test_a_failed_transition_list_is_returned(rest):
    rest["replies"]["/transitions"] = {"ok": False, "error": "http 404"}
    assert jira.jira_transitions({"key": "ENG-1"})["ok"] is False


@pytest.fixture()
def transitions(rest):
    rest["replies"]["/transitions"] = {"ok": True, "data": {"transitions": [
        {"id": "11", "name": "Start Progress", "to": {"name": "In Progress"}},
        {"id": "21", "name": "Close", "to": {"name": "Done"}}]}}
    return rest


@pytest.mark.parametrize("want", ["11", "Start Progress", "start progress",
                                  "In Progress", "in progress"])
def test_a_transition_matches_by_id_name_or_target_status(transitions, want):
    out = jira.jira_transition({"key": "ENG-1", "transition": want})
    assert out["ok"] is True and out["transitioned_to"] == want
    assert transitions["calls"][-1]["body"]["transition"] == {"id": "11"}


def test_a_transition_needs_a_target(rest):
    assert "missing 'transition'" in jira.jira_transition({"key": "ENG-1"})["error"]


def test_an_unmatched_transition_lists_the_options(transitions):
    out = jira.jira_transition({"key": "ENG-1", "transition": "Vanish"})
    assert out["ok"] is False
    assert out["available"] == ["Start Progress", "Close"]


def test_a_transition_comment_rides_along(transitions):
    jira.jira_transition({"key": "ENG-1", "transition": "Close", "comment": "done"})
    assert transitions["calls"][-1]["body"]["update"]["comment"] == [
        {"add": {"body": "done"}}]


def test_a_failed_transition_post_is_returned(transitions):
    listed = transitions["replies"]["/transitions"]
    calls = {"n": 0}

    def _reply(_state):
        calls["n"] += 1
        return listed if calls["n"] == 1 else {"ok": False, "error": "http 400"}
    transitions["replies"]["/transitions"] = _reply
    assert jira.jira_transition({"key": "ENG-1", "transition": "11"})["ok"] is False


# ─── assignment + links ────────────────────────────────────────────────


def test_an_issue_is_assigned(rest):
    out = jira.jira_assign({"key": "ENG-1", "assignee": "ada"})
    assert out["assignee"] == "ada"
    assert rest["calls"][0]["body"] == {"name": "ada"}


@pytest.mark.parametrize("who", ["-1", "unassigned", "NONE"])
def test_an_issue_can_be_unassigned(rest, who):
    out = jira.jira_assign({"key": "ENG-1", "assignee": who})
    assert out["assignee"] == "(unassigned)"
    assert rest["calls"][0]["body"] == {"name": None}


def test_assigning_needs_a_user(rest):
    assert jira.jira_assign({"key": "ENG-1"})["error"] == "missing 'assignee'"


def test_a_failed_assign_is_returned(rest):
    rest["default"] = {"ok": False, "error": "http 403"}
    assert jira.jira_assign({"key": "ENG-1", "assignee": "ada"})["ok"] is False


def test_two_issues_are_linked(rest):
    out = jira.jira_link_issues({"inward": "ENG-1", "outward": "ENG-2",
                                 "type": "Blocks", "comment": "why"})
    assert out["linked"] == {"inward": "ENG-1", "outward": "ENG-2",
                             "type": "Blocks"}
    body = rest["calls"][0]["body"]
    assert body["inwardIssue"] == {"key": "ENG-1"}
    assert body["comment"] == {"body": "why"}


def test_linking_defaults_to_relates(rest):
    jira.jira_link_issues({"from": "ENG-1", "to": "ENG-2"})
    assert rest["calls"][0]["body"]["type"] == {"name": "Relates"}


def test_linking_needs_both_keys(rest):
    assert "inward" in jira.jira_link_issues({"inward": "ENG-1"})["error"]


def test_a_failed_link_is_returned(rest):
    rest["default"] = {"ok": False, "error": "http 400"}
    assert jira.jira_link_issues({"inward": "A", "outward": "B"})["ok"] is False


# ─── the connectivity check ────────────────────────────────────────────


def test_an_unconfigured_jira_says_so(monkeypatch):
    monkeypatch.setattr(jira, "_configured", lambda: False)
    assert jira.jira_test() == {"ok": False, "error": "jira_not_configured"}


def test_a_working_token_reports_the_user(rest, monkeypatch):
    monkeypatch.setattr(jira, "_configured", lambda: True)
    monkeypatch.setattr(jira, "_auth_scheme", lambda: "bearer")
    monkeypatch.setattr(jira, "_base", lambda: "https://j")
    rest["replies"]["/myself"] = {"ok": True, "data": {"displayName": "Ada"}}
    out = jira.jira_test()
    assert out == {"ok": True, "base_url": "https://j", "auth": "bearer",
                   "user": "Ada"}


def test_a_pat_sent_as_basic_auth_gets_the_right_hint(rest, monkeypatch):
    """The most common misconfiguration: a PAT must go as Bearer."""
    monkeypatch.setattr(jira, "_configured", lambda: True)
    monkeypatch.setattr(jira, "_auth_scheme", lambda: "basic")
    monkeypatch.setattr(jira, "_base", lambda: "https://j")
    rest["replies"]["/myself"] = {"ok": False, "error": "http 401 unauthorized"}
    assert "clear the User field" in jira.jira_test()["hint"].replace("— c", "c")


def test_a_rejected_bearer_token_gets_its_own_hint(rest, monkeypatch):
    monkeypatch.setattr(jira, "_configured", lambda: True)
    monkeypatch.setattr(jira, "_auth_scheme", lambda: "bearer")
    monkeypatch.setattr(jira, "_base", lambda: "https://j")
    rest["replies"]["/myself"] = {"ok": False, "error": "http 403 forbidden"}
    assert "Personal Access Token" in jira.jira_test()["hint"]


def test_a_non_auth_failure_carries_no_hint(rest, monkeypatch):
    monkeypatch.setattr(jira, "_configured", lambda: True)
    monkeypatch.setattr(jira, "_auth_scheme", lambda: "bearer")
    monkeypatch.setattr(jira, "_base", lambda: "https://j")
    rest["replies"]["/myself"] = {"ok": False, "error": "connection refused"}
    assert "hint" not in jira.jira_test()
