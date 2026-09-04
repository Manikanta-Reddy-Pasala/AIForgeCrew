"""Internal subtask tracking (event-sourced)."""
import importlib

import pytest

from tests.python._adk_cb import run_cb


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
    assert len(got) == 4
    assert all(s["status"] == "pending" for s in got)
    subtasks.update_subtask(t.id, "schema", "done")
    subtasks.update_subtask(t.id, "models", "done")
    subtasks.update_subtask(t.id, "api", "running")
    cur = {s["slug"]: s["status"] for s in subtasks.get_subtasks(t.id)}
    assert cur == {"schema": "done", "models": "done", "api": "running", "tests": "pending"}
    p = subtasks.progress(subtasks.get_subtasks(t.id))
    assert p["total"] == 4
    assert p["done"] == 2
    assert abs(p["fraction"] - 0.5) < 1e-9


def test_invalid_status_rejected(env):
    store, subtasks = env
    t = store.create(title="x", body="y")
    assert subtasks.update_subtask(t.id, "a", "bogus")["ok"] is False


def test_set_subtasks_normalizes_status(env):
    # item 7a — a planner emitting "Done"/"In-Progress" must not leak an unknown
    # status that breaks progress(); normalize case + fall back to "pending".
    store, subtasks = env
    t = store.create(title="x", body="y")
    subtasks.set_subtasks(t.id, [
        {"slug": "a", "goal": "a", "status": "Done"},        # cased → done
        {"slug": "b", "goal": "b", "status": "In-Progress"}, # unknown → pending
        {"slug": "c", "goal": "c"},                          # missing → pending
    ])
    cur = {s["slug"]: s["status"] for s in subtasks.get_subtasks(t.id)}
    assert cur == {"a": "done", "b": "pending", "c": "pending"}
    p = subtasks.progress(subtasks.get_subtasks(t.id))
    assert p["counts"] == {"done": 1, "pending": 2}
    assert p["done"] == 1


def test_replan_resets(env):
    store, subtasks = env
    t = store.create(title="x", body="y")
    subtasks.set_subtasks(t.id, [{"slug": "a", "goal": "a"}])
    subtasks.update_subtask(t.id, "a", "done")
    subtasks.set_subtasks(t.id, [{"slug": "b", "goal": "b"}])   # new plan
    cur = subtasks.get_subtasks(t.id)
    assert [s["slug"] for s in cur] == ["b"]
    assert cur[0]["status"] == "pending"


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

    class Ctx:
        def __init__(self, plan):
            self.state = {"plan_md": plan, "ticket_identifier": t.identifier}

    plan = '{"subtickets": [{"slug": "a", "goal": "x"}, {"slug": "b", "goal": "y"}]}'
    callback = cb.make_planner_subtasks_callback()
    run_cb(callback, callback_context=Ctx(plan))
    subtasks.update_subtask(t.id, "a", "done")
    # re-run the callback with the SAME slugs → must NOT reset 'a' progress
    run_cb(callback, callback_context=Ctx(plan))
    cur = {s["slug"]: s["status"] for s in subtasks.get_subtasks(t.id)}
    assert cur["a"] == "done", "no-op replan wiped progress"


def test_extract_subtickets_from_markdown_phases():
    from aiforge_core.runtime.subtasks_callback import _extract_subtickets
    # planner often writes phases as numbered markdown in plan_md, not a
    # subtickets JSON array — must still decompose.
    plan = ('{"plan_md": "## Plan\\n### Phases (5)\\n'
            '1. **Project Init** — scaffold layout.\\n'
            '2. **DB Models** — define Url entity.\\n'
            '3. **Base62** — slug helpers.\\n'
            '4. **Routers** — POST /shorten, GET /{slug}.\\n'
            '5. **Tests** — pytest suite."}')
    subs = _extract_subtickets(plan)
    assert len(subs) == 5
    assert subs[0]["slug"] == "project-init"
    assert "scaffold" in subs[0]["goal"]
    # structured JSON array still takes priority
    assert len(_extract_subtickets('{"subtickets":[{"slug":"a","goal":"x"}'
                                    ',{"slug":"b","goal":"y"}]}')) == 2
    # a lone numbered line in prose is NOT a decomposition
    assert _extract_subtickets('{"plan_md":"do step 1. now"}') == []
