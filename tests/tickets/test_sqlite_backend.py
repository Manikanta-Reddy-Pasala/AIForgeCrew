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
