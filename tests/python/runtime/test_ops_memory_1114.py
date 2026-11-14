"""Operational memory: hosts (how to reach each) and repo run recipes."""
from __future__ import annotations

import pytest

from aiforge_core.runtime import ops_memory as O


@pytest.fixture(autouse=True)
def _cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("AIFORGE_OPS_MEMORY", raising=False)
    from aiforge_core.config import paths
    if hasattr(paths, "_reset_cache"):
        paths._reset_cache()
    yield


def _cmd(c, ok=True):
    return {"key": "cmd:" + c, "outcome": ok}


def test_two_hosts_by_name_or_ip_each_get_their_own_access():
    O.upsert_host("nuc", {"addresses": ["192.168.70.108"], "method": "ssh_key",
                          "user": "ai"}, source="user")
    O.upsert_host("elint", {"addresses": ["192.168.70.103"], "method": "password",
                            "secret": "elint_pass", "user": "root"}, source="user")
    by_ip = O.context_block("", "check disk on 192.168.70.103")
    assert "elint" in by_ip and "password" in by_ip and "nuc" not in by_ip
    assert "elint_pass" in by_ip and "never print it" in by_ip
    by_name = O.context_block("", "restart the service on NUC")
    assert "nuc" in by_name and "ssh_key" in by_name and "elint" not in by_name
    assert O.context_block("", "write a parser") == ""


def test_a_name_inside_another_word_does_not_match():
    O.upsert_host("ms", {"method": "ssh_key"}, source="user")
    assert O.find_hosts("the items list") == []
    assert O.find_hosts("run it on ms please")


def test_what_the_user_said_is_not_overwritten_by_learning():
    O.upsert_host("elint", {"addresses": ["10.0.0.3"], "method": "password"}, source="user")
    O.learn_from_items([_cmd("ssh root@10.0.0.3 'df -h'")])
    h = O.load()["hosts"]["elint"]
    assert h["method"] == "password"              # user's setting kept
    assert "ssh root@10.0.0.3" in h["recipe"]     # learned detail added


def test_ssh_failed_then_paramiko_worked_is_learned_with_the_lesson():
    O.learn_from_items([
        _cmd("ssh admin@10.1.1.5 'uptime'", ok=False),
        _cmd("python3 -c \"import paramiko; c=paramiko.SSHClient(); "
             "c.connect('10.1.1.5', username='admin')\""),
    ])
    h = O.load()["hosts"]["10.1.1.5"]
    assert h["method"] == "paramiko"
    assert any("use paramiko" in a for a in h["avoid"])
    assert "paramiko" in O.context_block("", "copy logs from 10.1.1.5")


def test_a_password_value_is_refused_as_a_secret_name():
    r = O.upsert_host("box", {"secret": "hunter2 with spaces!"}, source="user")
    assert not r["ok"]


def test_repo_recipe_is_learned_from_a_test_run_not_from_probes(tmp_path):
    root = str(tmp_path / "click")
    O.learn_from_items([
        _cmd(f"cd {root} && PYTHONPATH=src {root}/.venv/bin/python -m pytest tests -q -k shell"),
        _cmd(f"cd {root} && {root}/.venv/bin/python -c 'import click; print(1)'"),
    ], repo="click", root=root)
    r = O.load()["repos"]["click"]
    assert r["python"] == ".venv/bin/python" and r["env"] == "PYTHONPATH=src"
    assert r["test_cmd"].endswith("-m pytest tests -q")      # -k narrowing dropped
    block = O.context_block("click", "fix the completion bug")
    assert "PYTHONPATH=src" in block and ".venv/bin/python" in block


def test_the_off_switch(monkeypatch):
    O.upsert_host("nuc", {"method": "ssh_key"}, source="user")
    monkeypatch.setenv("AIFORGE_OPS_MEMORY", "0")
    assert O.context_block("", "on nuc") == ""


def test_the_tools_save_user_facts(tmp_path):
    from aiforge_core.runtime.chat_agent._tools import _t_remember_host, _t_remember_run
    assert _t_remember_host({"name": "nuc2", "addresses": "192.168.70.108, nuc2.lan",
                             "method": "ssh_key", "port": 22}, str(tmp_path))["ok"]
    h = O.load()["hosts"]["nuc2"]
    assert h["addresses"] == ["192.168.70.108", "nuc2.lan"] and h["port"] == "22"
    assert h["_source"]["method"] == "user"
    assert _t_remember_run({"python": "/opt/py/bin/python", "repo": "svc"}, str(tmp_path))["ok"]
    assert O.load()["repos"]["svc"]["python"] == "/opt/py/bin/python"
