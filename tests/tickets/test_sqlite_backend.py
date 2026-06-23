import pytest

from aiforge_core.tickets.backends.sqlite_backend import SqliteBackend


@pytest.fixture
def be(tmp_path):
    b = SqliteBackend(str(tmp_path / "t.db"))
    b.ensure_schema()
    return b


def _mk(be, *, priority="medium", project="demo", assignee="doer",
        title="t", parent_id=None):
    ident = f"ONE-{be.next_counter()}"
    return be.insert_ticket({
        "identifier": ident, "title": title, "body": "", "priority": priority,
        "assignee_role": assignee, "parent_id": parent_id, "project": project,
        "labels": [], "branch": None, "metadata": {},
        "route": "code", "route_workflow": None, "route_source": "auto",
        "route_confidence": None,
    })


def test_counter_starts_at_100(be):
    assert be.next_counter() == 100
    assert be.next_counter() == 101


def test_insert_and_fetch(be):
    ident = f"ONE-{be.next_counter()}"
    row = be.insert_ticket({
        "identifier": ident, "title": "hello", "body": "world",
        "priority": "high", "assignee_role": "doer", "parent_id": None,
        "project": "demo", "labels": ["x", "y"], "branch": None,
        "metadata": {"k": 1}, "route": "code", "route_workflow": None,
        "route_source": "auto", "route_confidence": None,
    })
    assert row["identifier"] == ident
    assert row["status"] == "todo"
    assert row["labels"] == ["x", "y"]
    assert row["metadata"] == {"k": 1}
    assert be.fetch_ticket(ident)["title"] == "hello"
    assert be.fetch_ticket(row["id"])["id"] == row["id"]
    # string identifiers are not treated as ids
    assert be.fetch_ticket("ONE-999") is None


def test_claim_oldest_respects_priority_then_age(be):
    low = _mk(be, priority="low")
    urgent = _mk(be, priority="urgent")
    claimed = be.claim_oldest(excluded_projects=[])
    assert claimed["id"] == urgent["id"]
    assert claimed["status"] == "in_progress"
    # next claim returns the remaining (low)
    assert be.claim_oldest(excluded_projects=[])["id"] == low["id"]


def test_claim_oldest_age_tiebreak_same_priority(be):
    first = _mk(be)
    _mk(be)
    assert be.claim_oldest(excluded_projects=[])["id"] == first["id"]


def test_claim_excludes_projects(be):
    _mk(be, project="TallyConnector")
    keep = _mk(be, project="demo")
    assert be.claim_oldest(excluded_projects=["TallyConnector"])["id"] == keep["id"]


def test_claim_null_project_always_claimable(be):
    t = _mk(be, project=None)
    assert be.claim_oldest(excluded_projects=["TallyConnector"])["id"] == t["id"]


def test_claim_returns_none_when_empty(be):
    assert be.claim_oldest(excluded_projects=[]) is None


def test_claim_is_atomic_no_double_claim(be):
    # one todo ticket → first claim takes it (flips to in_progress), a second
    # claim must NOT re-hand the same ticket (audit fix: atomic claim).
    t = _mk(be)
    first = be.claim_oldest(excluded_projects=[])
    assert first["id"] == t["id"] and first["status"] == "in_progress"
    assert be.claim_oldest(excluded_projects=[]) is None


def test_set_status_completed_and_metadata_merge(be):
    t = _mk(be)
    be.set_status(t["id"], "in_progress", completed=False, metadata_patch={"a": 1})
    out = be.set_status(t["id"], "done", completed=True, metadata_patch={"b": 2})
    assert out["status"] == "done"
    assert out["completed_at"] is not None
    assert out["metadata"] == {"a": 1, "b": 2}


def test_set_status_not_completed_keeps_completed_at_null(be):
    t = _mk(be)
    out = be.set_status(t["id"], "blocked", completed=False, metadata_patch={})
    assert out["completed_at"] is None


def test_set_route_by_id_and_identifier(be):
    t = _mk(be)
    out = be.set_route(t["id"], "workflow", "wf-1", "manual", 0.9)
    assert out["route"] == "workflow"
    assert out["route_workflow"] == "wf-1"
    assert out["route_confidence"] == 0.9
    out2 = be.set_route(t["identifier"], "code", None, "auto", None)
    assert out2["route"] == "code"


def test_events_all_kinds_oldest_first(be):
    t = _mk(be)
    e1 = be.insert_event(t["id"], "doer", "status_change", "in_progress", {})
    e2 = be.insert_event(t["id"], "doer", "comment", "hi", {"a": 1})
    assert e1 > 0 and e2 > 0
    evs = be.fetch_events(t["id"], limit=10)
    # ALL kinds returned, oldest-first, with agent_role key
    assert [e["kind"] for e in evs] == ["status_change", "comment"]
    assert evs[0]["agent_role"] == "doer"
    assert evs[1]["metadata"] == {"a": 1}


def test_children(be):
    parent = _mk(be, assignee="planner")
    child = _mk(be, parent_id=parent["id"])
    assert [k["id"] for k in be.fetch_children(parent["id"])] == [child["id"]]


def test_search_title_lower_and_status_filter(be):
    _mk(be, title="Unique-Title")
    cancelled = _mk(be, title="Unique-Title")
    be.set_status(cancelled["id"], "cancelled", completed=True, metadata_patch={})
    hits = be.search_title("unique-title", "demo",
                           ["todo", "in_progress", "done"])
    # cancelled excluded by status filter; case-insensitive match
    assert len(hits) == 1
