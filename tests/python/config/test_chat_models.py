"""The model each chat runs on: a pick in one chat is that chat's only."""
import pytest

from aiforge_core.config import agent_config as A
from aiforge_core.config import chat_models as C
from aiforge_core.runtime import request_context as R


@pytest.fixture(autouse=True)
def _cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    for var in ("AIFORGE_CHAT_MODEL", "AIFORGE_DOER_MODEL", "AIFORGE_PLANNER_MODEL",
                "AIFORGE_DEFAULT_MODEL", "AIFORGE_DEFAULT_PROVIDER",
                "AIFORGE_DEFAULT_BASE_URL", "AIFORGE_CHAT_BASE_URL",
                "AIFORGE_OPENAI_COMPAT_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    C._CACHE.update(key=None, data={})
    A.set_role("_default", "openai_compatible", "coder", base_url="http://boxA:1234/v1")
    A.set_role("chat", "openai_compatible", "coder", base_url="http://boxA:1234/v1")
    A.set_role("planner", "openai_compatible", "thinker", base_url="http://boxA:1234/v1")
    yield
    R.set_session_id(None)


def _in(sid, role):
    tok = R.set_session_id(sid)
    try:
        return A.get(role)
    finally:
        R.reset_session_id(tok)


def test_a_chat_pick_is_that_chats_only():
    C.set_session(5, "big-model")
    assert _in(5, "chat")["model"] == "big-model"
    assert _in(6, "chat")["model"] == "coder"
    assert A.get("chat")["model"] == "coder"           # no chat: the global pick


def test_roles_on_the_global_default_follow_the_chat_own_rows_do_not():
    C.set_session(5, "big-model")
    assert _in(5, "doer")["model"] == "big-model"       # inherits _default
    assert _in(5, "planner")["model"] == "thinker"      # its own model
    assert _in(5, "_default")["model"] == "big-model"
    assert A.resolve_litellm("doer")["model_id"].endswith("coder")
    tok = R.set_session_id(5)
    try:
        assert A.resolve_litellm("doer")["model_id"].endswith("big-model")
    finally:
        R.reset_session_id(tok)


def test_another_endpoint_takes_its_own_key_not_the_global_one():
    A.set_role("chat", "openai_compatible", "coder", base_url="http://boxA:1234/v1",
               api_key="sk-A")
    C.set_session(5, "remote", base_url="http://boxB:9/v1", api_key=None)
    row = _in(5, "chat")
    assert row["base_url"] == "http://boxB:9/v1" and not row.get("api_key")
    C.set_session(6, "other-on-A", base_url="http://boxA:1234/v1")
    assert _in(6, "chat")["api_key"] == "sk-A"           # same endpoint: same key


def test_an_env_pin_still_wins(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_MODEL", "pinned")
    C.set_session(5, "big-model")
    assert _in(5, "chat")["model"] == "pinned"


def test_forget_copy_and_delete_all():
    C.set_session(5, "big-model")
    C.copy_session(5, 7)
    assert C.get(7)["model"] == "big-model"
    C.forget_session(5)
    assert C.get(5) is None and _in(5, "chat")["model"] == "coder"
    C.forget_session(None)
    assert C.get(7) is None


def test_an_unreadable_file_falls_back_to_the_global_pick(tmp_path):
    (tmp_path / "chat-models.json").write_text("{not json")
    assert _in(5, "chat")["model"] == "coder"
