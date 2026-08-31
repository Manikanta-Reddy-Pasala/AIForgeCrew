"""The Confluence page tools: search, read, write, attach, labels, comments.

Two things shape this surface. A page WRITE goes through the storage-format
converter and then uploads any inline images as attachments — on update the
upload happens FIRST, so the ``<ri:attachment>`` references in the new body
resolve the moment the version is published. And a bare full-text search is
scoped to the default space, for the same reason the Jira one is scoped to a
project: unscoped it searches everything the token can see.

Every tool returns ``{"ok": bool, ...}``; the REST seam is stubbed.
"""
from __future__ import annotations

import sys

import pytest

from aiforge_core.runtime.tools import confluence
from aiforge_core.runtime.tools.confluence import _tools as ct


@pytest.fixture()
def rest(monkeypatch):
    state: dict = {"calls": [], "replies": {}, "default": {"ok": True, "data": {}}}

    def _request(method, path, params=None, body=None, **kw):
        state["calls"].append({"method": method, "path": path,
                               "params": params or {}, "body": body})
        for frag, rep in state["replies"].items():
            if frag in path:
                return rep(state) if callable(rep) else rep
        return state["default"]
    # The tools forward to the PACKAGE-level _request at call time.
    monkeypatch.setattr(sys.modules["aiforge_core.runtime.tools.confluence"],
                        "_request", _request)
    monkeypatch.setattr(ct, "default_space", lambda: "")
    monkeypatch.setattr(ct, "_page_url", lambda d: f"https://c/pages/{d.get('id')}")
    return state


# ─── search ────────────────────────────────────────────────────────────


def test_a_full_text_query_becomes_cql(rest):
    rest["replies"]["/search"] = {"ok": True, "data": {"results": [
        {"id": "1", "title": "Spec", "type": "page", "space": {"key": "ENG"}}]}}
    out = confluence.confluence_search({"query": 'say "hi"'})
    assert out["results"] == [{"id": "1", "title": "Spec", "type": "page",
                               "space": "ENG"}]
    assert rest["calls"][0]["params"]["cql"] == 'text ~ "say \\"hi\\""'


def test_a_bare_query_is_scoped_to_the_default_space(rest, monkeypatch):
    monkeypatch.setattr(ct, "default_space", lambda: "ENG")
    rest["replies"]["/search"] = {"ok": True, "data": {"results": []}}
    confluence.confluence_search({"query": "crash"})
    assert rest["calls"][0]["params"]["cql"] == 'space = "ENG" AND (text ~ "crash")'


def test_a_space_in_the_cql_is_left_alone(rest, monkeypatch):
    monkeypatch.setattr(ct, "default_space", lambda: "ENG")
    rest["replies"]["/search"] = {"ok": True, "data": {"results": []}}
    confluence.confluence_search({"cql": 'space = "OPS" AND text ~ "x"'})
    assert rest["calls"][0]["params"]["cql"] == 'space = "OPS" AND text ~ "x"'


def test_a_space_value_cannot_break_out_of_the_cql_literal(rest, monkeypatch):
    monkeypatch.setattr(ct, "default_space", lambda: 'E" OR 1=1 "')
    rest["replies"]["/search"] = {"ok": True, "data": {"results": []}}
    confluence.confluence_search({"query": "x"})
    assert rest["calls"][0]["params"]["cql"].startswith('space = "E\\" OR 1=1 \\""')


def test_a_search_needs_a_query_or_cql():
    assert confluence.confluence_search({})["error"] == "missing 'query' or 'cql'"


def test_a_failed_search_is_returned(rest):
    rest["replies"]["/search"] = {"ok": False, "error": "http 401"}
    assert confluence.confluence_search({"query": "x"})["ok"] is False


# ─── resolving a page ──────────────────────────────────────────────────


def test_an_explicit_id_needs_no_lookup(rest):
    assert ct._resolve_page_id({"id": "123"}) == "123"
    assert rest["calls"] == []


def test_a_title_is_looked_up_within_the_space(rest, monkeypatch):
    monkeypatch.setattr(ct, "default_space", lambda: "ENG")
    rest["replies"]["/rest/api/content"] = {"ok": True, "data": {
        "results": [{"id": "77"}]}}
    assert ct._resolve_page_id({"title": "Spec"}) == "77"
    assert rest["calls"][0]["params"]["spaceKey"] == "ENG"


def test_a_title_that_matches_nothing(rest):
    rest["replies"]["/rest/api/content"] = {"ok": True, "data": {"results": []}}
    assert ct._resolve_page_id({"title": "Ghost"}) == {"ok": False,
                                                       "error": "page_not_found"}


def test_neither_id_nor_title():
    assert ct._resolve_page_id({})["error"] == "missing 'id' or 'title'"


def test_a_failed_lookup_is_returned(rest):
    rest["replies"]["/rest/api/content"] = {"ok": False, "error": "http 500"}
    assert ct._resolve_page_id({"title": "Spec"})["ok"] is False


# ─── reading ───────────────────────────────────────────────────────────


def test_a_page_is_read_with_its_body_and_version(rest, monkeypatch):
    monkeypatch.setattr(sys.modules["aiforge_core.runtime.tools.confluence"],
                        "_fetch_attachments", lambda pid: [])
    rest["replies"]["/rest/api/content/1"] = {"ok": True, "data": {
        "id": "1", "title": "Spec", "space": {"key": "ENG"},
        "version": {"number": 3},
        "body": {"storage": {"value": "<p>hi</p>"}}}}
    out = confluence.confluence_read({"id": "1"})
    assert out["title"] == "Spec" and out["version"] == 3
    assert out["body"] == "<p>hi</p>" and out["space"] == "ENG"


def test_attachments_are_analysed_by_default(rest, monkeypatch):
    monkeypatch.setattr(sys.modules["aiforge_core.runtime.tools.confluence"],
                        "_fetch_attachments", lambda pid: [{"filename": "a.png"}])
    rest["replies"]["/rest/api/content/1"] = {"ok": True, "data": {"id": "1"}}
    assert confluence.confluence_read({"id": "1"})["attachments"] == [
        {"filename": "a.png"}]


def test_attachments_can_be_turned_off(rest, monkeypatch):
    monkeypatch.setattr(sys.modules["aiforge_core.runtime.tools.confluence"],
                        "_fetch_attachments",
                        lambda pid: pytest.fail("fetched with attachments=false"))
    rest["replies"]["/rest/api/content/1"] = {"ok": True, "data": {"id": "1"}}
    assert "attachments" not in confluence.confluence_read({"id": "1",
                                                            "attachments": "false"})


def test_a_failed_read_is_returned(rest):
    rest["replies"]["/rest/api/content/1"] = {"ok": False, "error": "http 404"}
    assert confluence.confluence_read({"id": "1"})["ok"] is False


# ─── creating ──────────────────────────────────────────────────────────


@pytest.fixture()
def writer(monkeypatch):
    monkeypatch.setattr(ct, "md_to_storage", lambda body: f"<p>{body}</p>")
    monkeypatch.setattr(ct, "_storagify_media",
                        lambda xhtml: (xhtml, ["img1.png"] if "img" in xhtml else []))
    uploads: list = []
    monkeypatch.setattr(ct, "_upload_page_images",
                        lambda pid, refs, cwd: uploads.append((pid, refs, cwd))
                        or [{"filename": "img1.png"}])
    return uploads


def test_a_page_is_created(rest, writer):
    rest["replies"]["/rest/api/content"] = {"ok": True, "data": {
        "id": "9", "title": "Spec"}}
    out = confluence.confluence_create({"title": "Spec", "space": "ENG",
                                        "body": "hello"})
    assert out["id"] == "9" and out["written"]["title"] == "Spec"
    body = rest["calls"][0]["body"]
    assert body["space"] == {"key": "ENG"}
    assert body["body"]["storage"]["value"] == "<p>hello</p>"


def test_the_default_space_fills_a_missing_one(rest, writer, monkeypatch):
    monkeypatch.setattr(ct, "default_space", lambda: "ENG")
    rest["replies"]["/rest/api/content"] = {"ok": True, "data": {"id": "9"}}
    confluence.confluence_create({"title": "t", "body": "b"})
    assert rest["calls"][0]["body"]["space"] == {"key": "ENG"}


@pytest.mark.parametrize("missing", ["title", "space", "body"])
def test_a_create_needs_its_required_fields(rest, writer, missing):
    args = {"title": "t", "space": "ENG", "body": "b"}
    args.pop(missing)
    assert confluence.confluence_create(args)["error"] == f"missing '{missing}'"


def test_a_parent_page_becomes_an_ancestor(rest, writer):
    rest["replies"]["/rest/api/content"] = {"ok": True, "data": {"id": "9"}}
    confluence.confluence_create({"title": "t", "space": "E", "body": "b",
                                  "parent_id": "5"})
    assert rest["calls"][0]["body"]["ancestors"] == [{"id": "5"}]


def test_inline_images_are_uploaded_after_the_page_exists(rest, writer):
    rest["replies"]["/rest/api/content"] = {"ok": True, "data": {"id": "9"}}
    out = confluence.confluence_create({"title": "t", "space": "E",
                                        "body": "see img"})
    assert out["attachments"] == [{"filename": "img1.png"}]
    assert writer[0][0] == "9"                # uploaded against the new page id


def test_a_failed_create_is_returned(rest, writer):
    rest["replies"]["/rest/api/content"] = {"ok": False, "error": "http 403"}
    assert confluence.confluence_create({"title": "t", "space": "E",
                                         "body": "b"})["ok"] is False


# ─── updating ──────────────────────────────────────────────────────────


def test_the_version_is_read_then_incremented(rest, writer):
    rest["replies"]["/rest/api/content/1"] = {"ok": True, "data": {
        "id": "1", "title": "Spec", "version": {"number": 4}}}
    out = confluence.confluence_update({"id": "1", "body": "new"})
    assert out["version"] == 5
    assert rest["calls"][-1]["body"]["version"] == {"number": 5}
    assert rest["calls"][-1]["body"]["title"] == "Spec"     # kept when not given


def test_a_first_version_starts_at_one(rest, writer):
    rest["replies"]["/rest/api/content/1"] = {"ok": True, "data": {"id": "1"}}
    assert confluence.confluence_update({"id": "1", "body": "b"})["version"] == 1


def test_images_upload_before_the_new_version_is_published(rest, writer):
    """Otherwise the <ri:attachment> references in the body do not resolve."""
    order: list = []
    rest["replies"]["/rest/api/content/1"] = lambda st: (
        order.append(st["calls"][-1]["method"]) or {"ok": True,
                                                    "data": {"id": "1"}})
    confluence.confluence_update({"id": "1", "body": "see img"})
    assert writer and order == ["GET", "PUT"]
    assert writer[0][0] == "1"


@pytest.mark.parametrize("args,err", [
    ({"body": "b"}, "missing 'id'"),
    ({"id": "1"}, "missing 'body'"),
])
def test_an_update_needs_an_id_and_a_body(args, err):
    assert confluence.confluence_update(args)["error"] == err


def test_an_unreadable_page_stops_the_update(rest, writer):
    rest["replies"]["/rest/api/content/1"] = {"ok": False, "error": "http 404"}
    assert confluence.confluence_update({"id": "1", "body": "b"})["ok"] is False


def test_a_failed_put_is_returned(rest, writer):
    calls = {"n": 0}

    def _reply(_st):
        calls["n"] += 1
        return ({"ok": True, "data": {"id": "1"}} if calls["n"] == 1
                else {"ok": False, "error": "version conflict"})
    rest["replies"]["/rest/api/content/1"] = _reply
    assert confluence.confluence_update({"id": "1", "body": "b"})["ok"] is False


# ─── attaching ─────────────────────────────────────────────────────────


def test_a_file_is_uploaded_as_an_attachment(rest, monkeypatch):
    monkeypatch.setattr(ct, "_resolve_image_bytes",
                        lambda src, cwd: (b"data", "image/png"))
    monkeypatch.setattr(ct, "_safe_filename", lambda src: "shot.png")
    seen: dict = {}
    monkeypatch.setattr(ct, "_upload_attachment",
                        lambda pid, name, data, ct_: seen.update(
                            pid=pid, name=name, ct=ct_) or {"ok": True})
    assert confluence.confluence_attach({"id": "1",
                                         "path": "/tmp/a.png"})["ok"] is True
    assert seen == {"pid": "1", "name": "shot.png", "ct": "image/png"}


def test_an_explicit_filename_overrides_the_derived_one(rest, monkeypatch):
    monkeypatch.setattr(ct, "_resolve_image_bytes", lambda src, cwd: (b"d", "image/png"))
    seen: dict = {}
    monkeypatch.setattr(ct, "_upload_attachment",
                        lambda pid, name, data, ct_: seen.setdefault("name", name)
                        and {"ok": True} or {"ok": True})
    confluence.confluence_attach({"id": "1", "url": "https://x/a.png",
                                  "filename": "diagram.png"})
    assert seen["name"] == "diagram.png"


def test_attaching_needs_a_page_and_a_source():
    assert confluence.confluence_attach({})["error"] == "missing 'id'"
    assert confluence.confluence_attach({"id": "1"})["error"] == "missing 'path' or 'url'"


def test_an_unreadable_source_is_reported(rest, monkeypatch):
    monkeypatch.setattr(ct, "_resolve_image_bytes", lambda src, cwd: None)
    out = confluence.confluence_attach({"id": "1", "path": "/gone.png"})
    assert out["ok"] is False and "could not read" in out["error"]


# ─── children, descendants, spaces ─────────────────────────────────────


def test_child_pages_are_listed(rest):
    rest["replies"]["/child/page"] = {"ok": True, "data": {"results": [
        {"id": "2", "title": "Child"}]}}
    out = confluence.confluence_children({"id": "1"})
    assert out["children"] == [{"id": "2", "title": "Child"}] and out["count"] == 1


def test_descendants_go_deep(rest):
    rest["replies"]["/descendant/page"] = {"ok": True, "data": {"results": [
        {"id": "3", "title": "Grandchild"}, "junk"]}}
    assert confluence.confluence_descendants({"id": "1"})["count"] == 1


@pytest.mark.parametrize("fn", ["confluence_children", "confluence_descendants",
                                "confluence_labels", "confluence_comments"])
def test_page_scoped_tools_need_an_id(fn):
    assert getattr(confluence, fn)({})["error"] == "missing 'id'"


@pytest.mark.parametrize("fn,frag", [
    ("confluence_children", "/child/page"),
    ("confluence_descendants", "/descendant/page"),
    ("confluence_labels", "/label"),
    ("confluence_comments", "/child/comment"),
])
def test_a_failed_page_scoped_read_is_returned(rest, fn, frag):
    rest["replies"][frag] = {"ok": False, "error": "http 403"}
    assert getattr(confluence, fn)({"id": "1"})["ok"] is False


def test_spaces_are_listed(rest):
    rest["replies"]["/rest/api/space"] = {"ok": True, "data": {"results": [
        {"key": "ENG", "name": "Engineering", "type": "global"}, "junk"]}}
    assert confluence.confluence_spaces({})["spaces"] == [
        {"key": "ENG", "name": "Engineering", "type": "global"}]


def test_a_failed_space_list_is_returned(rest):
    rest["replies"]["/rest/api/space"] = {"ok": False, "error": "http 401"}
    assert confluence.confluence_spaces({})["ok"] is False


def test_a_loose_space_name_resolves_to_its_key(rest, monkeypatch):
    rest["replies"]["/rest/api/space"] = {"ok": True, "data": {"results": [
        {"key": "ENG", "name": "Engineering"}]}}
    seen: dict = {}

    def _pick(name, cands, value_key=None):
        seen.update(cands=cands)
        return {"ok": True, "key": "ENG"}
    monkeypatch.setattr("aiforge_core.config.repo_map.fuzzy_pick", _pick)
    assert confluence.confluence_resolve_space({"name": "enginering"})["key"] == "ENG"
    assert seen["cands"] == {"ENG": "ENG", "Engineering": "ENG"}


def test_resolving_a_space_needs_a_name():
    assert confluence.confluence_resolve_space({})["error"] == "missing 'name'"


def test_an_unreachable_instance_cannot_resolve_a_space(rest):
    rest["replies"]["/rest/api/space"] = {"ok": False, "error": "http 500"}
    assert confluence.confluence_resolve_space({"name": "eng"})["ok"] is False


# ─── page by title ─────────────────────────────────────────────────────


def test_a_page_is_found_by_exact_title(rest):
    rest["replies"]["/rest/api/content"] = {"ok": True, "data": {"results": [
        {"id": "1", "title": "Spec", "version": {"number": 2}}]}}
    out = confluence.confluence_page_by_title({"space": "ENG", "title": "Spec"})
    assert out["found"] is True and out["id"] == "1" and out["version"] == 2


def test_a_title_that_exists_nowhere_is_not_an_error(rest):
    rest["replies"]["/rest/api/content"] = {"ok": True, "data": {"results": []}}
    out = confluence.confluence_page_by_title({"space": "ENG", "title": "Ghost"})
    assert out == {"ok": True, "found": False, "space": "ENG", "title": "Ghost"}


def test_finding_by_title_needs_both_space_and_title(rest):
    assert confluence.confluence_page_by_title({"title": "x"})["ok"] is False
    assert confluence.confluence_page_by_title({"space": "E"})["ok"] is False


def test_a_failed_title_lookup_is_returned(rest):
    rest["replies"]["/rest/api/content"] = {"ok": False, "error": "http 500"}
    assert confluence.confluence_page_by_title({"space": "E",
                                                "title": "t"})["ok"] is False


# ─── labels + comments ─────────────────────────────────────────────────


def test_labels_are_read(rest):
    rest["replies"]["/label"] = {"ok": True, "data": {"results": [
        {"name": "spec"}, {"noname": 1}, "junk"]}}
    assert confluence.confluence_labels({"id": "1"})["labels"] == ["spec"]


@pytest.mark.parametrize("labels,added", [
    (["a", "b"], ["a", "b"]),
    ("a, b ,,c", ["a", "b", "c"]),
])
def test_labels_are_added(rest, labels, added):
    out = confluence.confluence_add_label({"id": "1", "labels": labels})
    assert out["added"] == added
    assert rest["calls"][0]["body"][0] == {"prefix": "global", "name": added[0]}


def test_adding_labels_needs_both_arguments(rest):
    assert confluence.confluence_add_label({"id": "1"})["ok"] is False
    assert confluence.confluence_add_label({"labels": ["a"]})["ok"] is False


def test_a_failed_label_write_is_returned(rest):
    rest["replies"]["/label"] = {"ok": False, "error": "http 403"}
    assert confluence.confluence_add_label({"id": "1", "labels": "a"})["ok"] is False


def test_comments_are_read_with_their_bodies(rest):
    rest["replies"]["/child/comment"] = {"ok": True, "data": {"results": [
        {"id": "5", "body": {"storage": {"value": "looks good"}}}, "junk"]}}
    out = confluence.confluence_comments({"id": "1"})
    assert out["comments"] == [{"id": "5", "body": "looks good"}]


def test_a_comment_is_posted_against_its_page(rest):
    rest["replies"]["/rest/api/content"] = {"ok": True, "data": {"id": "7"}}
    out = confluence.confluence_comment({"id": "1", "text": "nice"})
    assert out == {"ok": True, "id": "7", "page_id": "1"}
    body = rest["calls"][0]["body"]
    assert body["container"] == {"id": "1", "type": "page"}
    assert body["body"]["storage"]["value"] == "nice"


def test_posting_a_comment_needs_an_id_and_a_body(rest):
    assert confluence.confluence_comment({"id": "1"})["ok"] is False
    assert confluence.confluence_comment({"body": "x"})["ok"] is False


def test_a_failed_comment_post_is_returned(rest):
    rest["replies"]["/rest/api/content"] = {"ok": False, "error": "http 403"}
    assert confluence.confluence_comment({"id": "1", "body": "x"})["ok"] is False


# ─── the connectivity check ────────────────────────────────────────────


def test_an_unconfigured_confluence_says_so(monkeypatch):
    monkeypatch.setattr(ct, "_configured", lambda: False)
    assert confluence.confluence_test() == {"ok": False,
                                            "error": "confluence_not_configured"}


def test_a_working_token_reports_the_base_url(rest, monkeypatch):
    monkeypatch.setattr(ct, "_configured", lambda: True)
    monkeypatch.setattr(ct, "_auth_scheme", lambda: "bearer")
    monkeypatch.setattr(ct, "_base", lambda: "https://c")
    rest["replies"]["/rest/api/space"] = {"ok": True, "data": {}}
    assert confluence.confluence_test() == {"ok": True, "base_url": "https://c",
                                            "auth": "bearer"}


def test_a_pat_sent_as_basic_auth_gets_a_hint(rest, monkeypatch):
    monkeypatch.setattr(ct, "_configured", lambda: True)
    monkeypatch.setattr(ct, "_auth_scheme", lambda: "basic")
    monkeypatch.setattr(ct, "_base", lambda: "https://c")
    rest["replies"]["/rest/api/space"] = {"ok": False, "error": "http 401 nope"}
    assert "Bearer" in confluence.confluence_test()["hint"]


def test_a_rejected_bearer_token_is_told_about_the_wiki_path(rest, monkeypatch):
    monkeypatch.setattr(ct, "_configured", lambda: True)
    monkeypatch.setattr(ct, "_auth_scheme", lambda: "bearer")
    monkeypatch.setattr(ct, "_base", lambda: "https://c")
    rest["replies"]["/rest/api/space"] = {"ok": False, "error": "http 403 nope"}
    assert "/wiki is Cloud only" in confluence.confluence_test()["hint"]


def test_a_non_auth_failure_carries_no_hint(rest, monkeypatch):
    monkeypatch.setattr(ct, "_configured", lambda: True)
    monkeypatch.setattr(ct, "_auth_scheme", lambda: "bearer")
    monkeypatch.setattr(ct, "_base", lambda: "https://c")
    rest["replies"]["/rest/api/space"] = {"ok": False, "error": "timed out"}
    assert "hint" not in confluence.confluence_test()
