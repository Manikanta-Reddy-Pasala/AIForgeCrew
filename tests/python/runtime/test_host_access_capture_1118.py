"""Two hosts, two access methods, said once by the user: each is kept for
its own host, and shown on every later turn of the chat."""
import pytest

from aiforge_core.runtime import ops_memory as om


@pytest.fixture(autouse=True)
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("AIFORGE_OPS_MEMORY", raising=False)


def _host(addr):
    k = om.host_for_address(addr)
    return om.load()["hosts"][k] if k else None


def test_key_for_one_password_for_the_other():
    om.capture_from_text("For 10.10.120.13 always use the SSH key. "
                         "The other IP 10.10.120.20 always uses username and password, user is builder.")
    a, b = _host("10.10.120.13"), _host("10.10.120.20")
    assert a["method"] == "ssh_key" and a["_source"]["method"] == "user"
    assert b["method"] == "password" and b["user"] == "builder"


def test_user_at_host_and_negation():
    om.capture_from_text("ssh ops@10.0.0.7 with key, not password")
    h = _host("10.0.0.7")
    assert h["method"] == "ssh_key" and h["user"] == "ops"


def test_a_clause_naming_both_or_neither_is_not_a_statement():
    om.capture_from_text("10.0.0.8 has a key and a password somewhere. 10.0.0.9 is the db box.")
    assert _host("10.0.0.8") is None and _host("10.0.0.9") is None


def test_learned_commands_never_override_what_the_user_said():
    om.capture_from_text("10.0.0.5 use username/password")
    om.upsert_host("10.0.0.5", {"method": "ssh_key"}, source="learned")
    assert _host("10.0.0.5")["method"] == "password"


def test_a_follow_up_without_the_ip_still_gets_the_host():
    from aiforge_core.runtime.chat_agent._turn import _blocks
    got = {}
    msgs = [{"role": "user", "content": "deploy on 10.0.0.5 — it uses the ssh key"},
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "now restart the service there"}]
    _blocks._append_session_blocks(lambda k, v: got.__setitem__(k, v), "/tmp", msgs, None, "chat")
    assert "10.0.0.5" in got.get("ops", "") and "ssh_key" in got["ops"]
    assert "use exactly this" in got["ops"]
