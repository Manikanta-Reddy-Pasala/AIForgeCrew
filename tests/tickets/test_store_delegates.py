import importlib

import pytest


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_PG_URL", raising=False)
    monkeypatch.delenv("AIFORGE_FORCE_PG", raising=False)
    monkeypatch.setenv("AIFORGE_DB_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("AIFORGE_RUNNER_EXCLUDE_PROJECTS", "TallyConnector")
    import aiforge_core.config.env as envmod
    importlib.reload(envmod)
    import aiforge_core.tickets.backend_factory as bf
    importlib.reload(bf)
    bf.reset_backend_for_tests()
    import aiforge_core.tickets.store as store
    importlib.reload(store)
    return store


def test_create_get_roundtrip(store):
    t = store.create(title="hello", body="body", project="demo",
                     assignee_role="doer", labels=["a"])
    assert t.identifier == "ONE-100"
    got = store.get(t.identifier)
    assert got.title == "hello"
    assert got.labels == ["a"]
    assert store.get(t.id).id == t.id


def test_default_assignee_is_supervisor(store):
    t = store.create(title="no assignee")
    assert t.assignee_role == "supervisor"


def test_urgent_keyword_boost(store):
    t = store.create(title="prod outage now", assignee_role="doer")
    assert t.priority == "urgent"
    assert t.metadata.get("priority_auto_boosted") is True


def test_dangerous_pattern_forces_supervisor(store):
    t = store.create(title="cleanup", body="please drop table users",
                     assignee_role="doer")
    assert t.assignee_role == "supervisor"
    assert "review-required" in t.labels
    assert t.metadata.get("dangerous_pattern") is True


def test_claim_priority_and_event(store):
    store.create(title="low one", assignee_role="doer", project="demo",
                 priority="low")
    urgent = store.create(title="urgent one", assignee_role="doer",
                          project="demo", priority="urgent")
    claimed = store.claim_next_any()
    assert claimed.id == urgent.id
    assert claimed.status == "in_progress"
    # claim wrote a status_change event
    evs = store.comments(claimed.id, limit=50)
    assert any(e["kind"] == "status_change" and e["agent_role"] == "graph_runner"
               for e in evs)


def test_claim_skips_excluded_project(store):
    store.create(title="tally job", assignee_role="doer",
                 project="TallyConnector")
    keep = store.create(title="keep", assignee_role="doer", project="demo")
    assert store.claim_next_any().id == keep.id


def test_update_status_done_sets_completed_and_event(store):
    t = store.create(title="work", assignee_role="doer", project="demo")
    done = store.update_status(t.id, "done", role="doer")
    assert done.status == "done"
    assert done.completed_at is not None
    evs = store.comments(t.id, limit=50)
    assert any(e["kind"] == "status_change" and e["body"] == "done" for e in evs)


def test_update_status_rejects_bad_status(store):
    t = store.create(title="x", assignee_role="doer")
    with pytest.raises(ValueError):
        store.update_status(t.id, "nonsense")


def test_comments_returns_all_event_kinds(store):
    t = store.create(title="x", assignee_role="doer", project="demo")
    store.add_event(t.id, "doer", "note", "a note", {"k": 1})
    store.add_comment(t.id, "doer", "a comment")
    kinds = {e["kind"] for e in store.comments(t.id, limit=50)}
    assert {"note", "comment"} <= kinds


def test_children_and_child_skips_invariants(store):
    parent = store.create(title="parent", assignee_role="planner", project="demo")
    child = store.create(title="child", parent_id=parent.id, project="demo")
    # child with no assignee should NOT be forced to supervisor
    assert child.assignee_role is None
    assert [c.id for c in store.children(parent.id)] == [child.id]


def test_by_title_project_case_insensitive_active_only(store):
    store.create(title="Dup Title", assignee_role="doer", project="demo")
    cancelled = store.create(title="Dup Title", assignee_role="doer",
                             project="demo")
    store.update_status(cancelled.id, "cancelled")
    hits = store.by_title_project("dup title", "demo")
    assert len(hits) == 1


def test_update_route(store):
    t = store.create(title="x", assignee_role="doer", project="demo")
    out = store.update_route(t.id, route="workflow", route_workflow="wf-1")
    assert out.route == "workflow"
    assert out.route_workflow == "wf-1"
    assert out.route_source == "manual"
