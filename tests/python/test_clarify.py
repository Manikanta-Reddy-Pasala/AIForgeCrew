import importlib

import pytest


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_PG_URL", raising=False)
    monkeypatch.setenv("AIFORGE_DB_PATH", str(tmp_path / "t.db"))
    import aiforge_core.config.env as envmod; importlib.reload(envmod)
    import aiforge_core.tickets.backend_factory as bf; importlib.reload(bf)
    bf.reset_backend_for_tests()
    import aiforge_core.tickets.store as store; importlib.reload(store)
    return store


def test_append_body(store):
    t = store.create(title="x", body="orig", assignee_role="doer", project="demo")
    store.append_body(t.id, "\nmore")
    assert store.get(t.id).body == "orig\nmore"


def test_clarify_skips_non_interactive(store, monkeypatch):
    import aiforge_core.runtime.clarify as cl; importlib.reload(cl)
    t = store.create(title="x", body="do thing", assignee_role="doer", project="demo")
    # not interactive → never asks
    assert cl.maybe_clarify(t) is False


def test_clarify_asks_then_parks(store, monkeypatch):
    import aiforge_core.runtime.clarify as cl; importlib.reload(cl)
    monkeypatch.setattr(cl, "_ask_llm", lambda t: ["Which file?", "What framework?"])
    t = store.create(title="x", body="vague", assignee_role="doer", project="demo",
                     metadata={"interactive": True})
    assert cl.maybe_clarify(t) is True
    got = store.get(t.id)
    assert got.status == "blocked"
    assert got.metadata.get("awaiting_input") is True
    assert len(got.metadata.get("clarify_questions")) == 2


def test_clarify_clear_proceeds(store, monkeypatch):
    import aiforge_core.runtime.clarify as cl; importlib.reload(cl)
    monkeypatch.setattr(cl, "_ask_llm", lambda t: [])
    t = store.create(title="x", body="clear req", assignee_role="doer",
                     project="demo", metadata={"interactive": True})
    assert cl.maybe_clarify(t) is False
    assert store.get(t.id).metadata.get("clarified") is True


def test_clarify_not_reasked_when_clarified(store, monkeypatch):
    import aiforge_core.runtime.clarify as cl; importlib.reload(cl)
    called = {"n": 0}
    monkeypatch.setattr(cl, "_ask_llm",
                        lambda t: called.__setitem__("n", called["n"] + 1) or ["q?"])
    t = store.create(title="x", body="v", assignee_role="doer", project="demo",
                     metadata={"interactive": True, "clarified": True})
    assert cl.maybe_clarify(t) is False
    assert called["n"] == 0
