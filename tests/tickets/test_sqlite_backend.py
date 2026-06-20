import pytest

from aiforge_core.tickets.backends.sqlite_backend import SqliteBackend


@pytest.fixture
def be(tmp_path):
    b = SqliteBackend(str(tmp_path / "t.db"))
    b.ensure_schema()
    return b


def test_new_identifier_increments(be):
    a = be.new_identifier()
    b = be.new_identifier()
    assert a == "ONE-1"
    assert b == "ONE-2"


def test_create_and_get(be):
    ident = be.new_identifier()
    row = be.create({
        "identifier": ident, "title": "hello", "body": "world",
        "status": "todo", "priority": "medium", "assignee_role": "doer",
        "parent_id": None, "branch": None, "project": "demo",
        "labels": ["x", "y"], "metadata": {"k": 1},
        "route": "code", "route_workflow": None,
        "route_source": "auto", "route_confidence": None,
    })
    assert row["identifier"] == ident
    assert row["labels"] == ["x", "y"]
    assert row["metadata"] == {"k": 1}
    got = be.get(ident)
    assert got["title"] == "hello"
    got_by_id = be.get(row["id"])
    assert got_by_id["id"] == row["id"]


def _mk(be, role, status="todo", project="demo"):
    ident = be.new_identifier()
    return be.create({
        "identifier": ident, "title": "t", "body": "", "status": status,
        "priority": "medium", "assignee_role": role, "parent_id": None,
        "branch": None, "project": project, "labels": [], "metadata": {},
        "route": "code", "route_workflow": None, "route_source": "auto",
        "route_confidence": None,
    })


def test_claim_next_any_oldest_first(be):
    first = _mk(be, "doer")
    _mk(be, "doer")
    claimed = be.claim_next_any(aliases=["doer"], excluded_projects=[])
    assert claimed["id"] == first["id"]


def test_claim_excludes_projects(be):
    _mk(be, "doer", project="skipme")
    keep = _mk(be, "doer", project="demo")
    claimed = be.claim_next_any(aliases=["doer"], excluded_projects=["skipme"])
    assert claimed["id"] == keep["id"]


def test_update_status_sets_completed(be):
    t = _mk(be, "doer")
    out = be.update_status(t["id"], "done", role="doer", extra={})
    assert out["status"] == "done"
    assert out["completed_at"] is not None


def test_update_route(be):
    t = _mk(be, "doer")
    out = be.update_route(t["id"], "workflow", "wf-1", "manual", 0.9)
    assert out["route"] == "workflow"
    assert out["route_workflow"] == "wf-1"
    assert out["route_confidence"] == 0.9


def test_events_and_comments(be):
    t = _mk(be, "doer")
    eid = be.add_event(t["id"], "doer", "note", "did a thing", {"a": 1})
    assert eid > 0
    cid = be.add_comment(t["id"], "doer", "a comment")
    assert cid > 0
    cs = be.comments(t["id"], limit=10)
    assert any(c["body"] == "a comment" for c in cs)


def test_children(be):
    parent = _mk(be, "planner")
    ident = be.new_identifier()
    child = be.create({
        "identifier": ident, "title": "c", "body": "", "status": "todo",
        "priority": "medium", "assignee_role": "doer", "parent_id": parent["id"],
        "branch": None, "project": "demo", "labels": [], "metadata": {},
        "route": "code", "route_workflow": None, "route_source": "auto",
        "route_confidence": None,
    })
    kids = be.children(parent["id"])
    assert [k["id"] for k in kids] == [child["id"]]


def test_by_title_project(be):
    _mk(be, "doer")
    ident = be.new_identifier()
    be.create({
        "identifier": ident, "title": "unique-title", "body": "", "status": "todo",
        "priority": "medium", "assignee_role": "doer", "parent_id": None,
        "branch": None, "project": "demo", "labels": [], "metadata": {},
        "route": "code", "route_workflow": None, "route_source": "auto",
        "route_confidence": None,
    })
    hits = be.by_title_project("unique-title", "demo")
    assert len(hits) == 1
