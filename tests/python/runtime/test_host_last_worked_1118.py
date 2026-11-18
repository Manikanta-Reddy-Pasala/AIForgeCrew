"""Per host: what last worked is kept and offered first; an access failure is
kept as last failed; a password the user types goes to the secret store."""
import pytest

from aiforge_core.runtime import ops_memory as om


@pytest.fixture(autouse=True)
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_SECURITY_DIR", str(tmp_path / "sec"))
    monkeypatch.delenv("AIFORGE_OPS_MEMORY", raising=False)


def _h(addr):
    return om.load()["hosts"][om.host_for_address(addr)]


def test_last_worked_wins_and_is_shown_first():
    om.host_hook("run_command", {"cmd": "ssh -i ~/.ssh/k ops@10.0.0.5 'uptime'"},
                 {"ok": True, "code": 0, "stdout": "up"})
    h = _h("10.0.0.5")
    assert h["last_ok"]["cmd"] == "ssh -i ~/.ssh/k ops@10.0.0.5 '<command>'"
    assert h["method"] == "ssh_key"
    block = om.context_block("", "restart it on 10.0.0.5")
    assert "LAST WORKED" in block and "ops@10.0.0.5" in block and "in order" in block


def test_access_failure_is_kept_and_cleared_by_a_later_success():
    cmd = {"cmd": "ssh root@10.0.0.6 'ls'"}
    om.host_hook("run_command", cmd, {"ok": False, "code": 255,
                                      "stderr": "root@10.0.0.6: Permission denied (publickey)."})
    h = _h("10.0.0.6")
    assert "Permission denied" in h["last_fail"]["error"] and h["avoid"]
    om.host_hook("run_command", {"cmd": "sshpass -p s3cret ssh root@10.0.0.6 'ls'"},
                 {"ok": True, "code": 0})
    h = _h("10.0.0.6")
    assert h["method"] == "password" and "s3cret" not in str(h)
    assert h["last_ok"]["cmd"].startswith("sshpass -p <password> ssh")
    assert "last failed" not in om.context_block("", "10.0.0.6")


def test_a_failing_remote_command_is_not_an_access_failure():
    om.host_hook("run_command", {"cmd": "ssh ops@10.0.0.7 'make test'"},
                 {"ok": False, "code": 2, "stderr": "make: *** [test] Error 1"})
    assert om.host_for_address("10.0.0.7") is None


def test_a_script_naming_a_saved_host_counts():
    om.capture_from_text("buildvm 10.0.0.9 uses username and password")
    om.host_hook("run_command", {"cmd": "python3 vm_exec.py 10.0.0.9 'cd x && make'"},
                 {"ok": True, "code": 0})
    h = _h("10.0.0.9")
    assert h["last_ok"]["cmd"] == "python3 vm_exec.py 10.0.0.9 '<command>'"
    assert h["method"] == "password"                    # the user's setting stays


def test_password_typed_by_the_user_goes_to_the_secret_store(tmp_path):
    om.capture_from_text("10.0.0.20 use username and password, user builder, password is Pa55-w0rd.")
    h = _h("10.0.0.20")
    assert h["secret"] == "host-10.0.0.20-password" and "Pa55" not in str(om.load())
    from aiforge_core.config import secure_store
    assert (secure_store.security_dir() / "secrets" / h["secret"]).read_text() == "Pa55-w0rd"
