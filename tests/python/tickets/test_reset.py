"""Delete-all + sequence reset for tickets (and chat). Memory/skills/workflows
are separate stores — NOT touched by these resets."""
import importlib
import pytest


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_TICKETS_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_DB_PATH", str(tmp_path / "t.db"))
    from aiforge_core.tickets import backend_factory
    backend_factory.reset_backend_for_tests()   # bind a fresh backend to tmp db
    from aiforge_core.tickets import store as s
    importlib.reload(s)
    yield s
    backend_factory.reset_backend_for_tests()   # don't leak to later tests


def test_reset_all_clears_and_restarts_sequence(store):
    for i in range(3):
        store.create(title=f"t{i}", body="x")
    assert store.reset_all() >= 3
    assert store.list_tickets(None, None, None, 100) == []
    # reset rewinds the counter to its seed → next ticket is ONE-100
    assert store.create(title="fresh", body="y").identifier == "ONE-100"


def test_chat_reset_restarts_session_ids(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_DB_PATH", str(tmp_path / "c.db"))
    from aiforge_core.runtime import chat_store
    importlib.reload(chat_store)
    chat_store.create_session("a"); chat_store.create_session("b")
    assert chat_store.delete_all_sessions() == 2
    assert chat_store.list_sessions() == []
    assert chat_store.create_session("new")["id"] == 1
