"""Internal subtask tracking (event-sourced)."""
import os
import importlib
import pytest


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_TICKETS_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_DB_PATH", str(tmp_path / "t.db"))
    from aiforge_core.tickets import store, subtasks
    importlib.reload(store); importlib.reload(subtasks)
    return store, subtasks


def test_set_get_update_progress(env):
    store, subtasks = env
    t = store.create(title="Big", body="x" * 3000, project="p")
    subtasks.set_subtasks(t.id, [
        {"slug": "schema", "goal": "tables"}, {"slug": "models", "goal": "orm"},
        {"slug": "api", "goal": "routes"}, {"slug": "tests", "goal": "tests"}])
    got = subtasks.get_subtasks(t.id)
    assert len(got) == 4 and all(s["status"] == "pending" for s in got)
    subtasks.update_subtask(t.id, "schema", "done")
    subtasks.update_subtask(t.id, "models", "done")
    subtasks.update_subtask(t.id, "api", "running")
    cur = {s["slug"]: s["status"] for s in subtasks.get_subtasks(t.id)}
    assert cur == {"schema": "done", "models": "done", "api": "running", "tests": "pending"}
    p = subtasks.progress(subtasks.get_subtasks(t.id))
    assert p["total"] == 4 and p["done"] == 2 and abs(p["fraction"] - 0.5) < 1e-9


def test_invalid_status_rejected(env):
    store, subtasks = env
    t = store.create(title="x", body="y")
    assert subtasks.update_subtask(t.id, "a", "bogus")["ok"] is False


def test_replan_resets(env):
    store, subtasks = env
    t = store.create(title="x", body="y")
    subtasks.set_subtasks(t.id, [{"slug": "a", "goal": "a"}])
    subtasks.update_subtask(t.id, "a", "done")
    subtasks.set_subtasks(t.id, [{"slug": "b", "goal": "b"}])   # new plan
    cur = subtasks.get_subtasks(t.id)
    assert [s["slug"] for s in cur] == ["b"] and cur[0]["status"] == "pending"


def test_callback_extracts_subtickets_from_plan_json():
    from aiforge_core.runtime.subtasks_callback import _extract_subtickets
    plan = '{"subtickets": [{"slug": "a", "goal": "x"}, {"slug": "b", "goal": "y"}]}'
    subs = _extract_subtickets(plan)
    assert [s["slug"] for s in subs] == ["a", "b"]
    # fenced in markdown
    md = "Here is the plan:\n```json\n" + plan + "\n```\ndone."
    assert len(_extract_subtickets(md)) == 2
    assert _extract_subtickets("no json here") == []


def test_callback_skips_noop_replan(env, monkeypatch):
    store, subtasks = env
    t = store.create(title="Big", body="x" * 3000, project="p")
    from aiforge_core.runtime import subtasks_callback as cb
    import asyncio

    class Ctx:
        def __init__(self, plan):
            self.state = {"plan_md": plan, "ticket_identifier": t.identifier}

    plan = '{"subtickets": [{"slug": "a", "goal": "x"}, {"slug": "b", "goal": "y"}]}'
    callback = cb.make_planner_subtasks_callback()
    asyncio.run(callback(callback_context=Ctx(plan)))
    subtasks.update_subtask(t.id, "a", "done")
    # re-run the callback with the SAME slugs → must NOT reset 'a' progress
    asyncio.run(callback(callback_context=Ctx(plan)))
    cur = {s["slug"]: s["status"] for s in subtasks.get_subtasks(t.id)}
    assert cur["a"] == "done", "no-op replan wiped progress"
