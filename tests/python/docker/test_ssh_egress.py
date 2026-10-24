"""docker/isolated/ssh-egress.py — the box's SSH exit: CONNECT to SSH ports
only; any host by default, allowlisted hosts or none when configured; end to
end through docker/ssh-connect.py (the box's ProxyCommand)."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
def eg(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location(
        "ssh_egress", REPO / "docker" / "isolated" / "ssh-egress.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "POLICY", str(tmp_path))
    monkeypatch.delenv("AIFORGE_SSH_EGRESS", raising=False)
    monkeypatch.delenv("AIFORGE_SSH_PORTS", raising=False)
    return mod


def test_ssh_port_only(eg):
    assert eg.allowed("github.com", 22) is None
    assert "only SSH ports" in eg.allowed("github.com", 443)
    assert eg.allowed("10.0.0.5", 22) is None                    # LAN ssh is fine
    for bad in ("127.0.0.1", "localhost", "169.254.169.254", "::1", "a b"):
        assert eg.allowed(bad, 22), bad


def test_modes(eg, monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_SSH_EGRESS", "off")
    assert "off" in eg.allowed("github.com", 22)
    monkeypatch.setenv("AIFORGE_SSH_EGRESS", "allowlist")
    (tmp_path / "hosts.json").write_text(json.dumps({"hosts": ["github.com"]}))
    assert eg.allowed("github.com", 22) is None
    assert eg.allowed("ssh.github.com", 22) is None
    assert "not on the network allowlist" in eg.allowed("gitlab.com", 22)
    monkeypatch.setenv("AIFORGE_SSH_PORTS", "22,2222")
    assert eg.allowed("github.com", 2222) is None


def _echo_server():
    """Stands in for sshd: answers with a banner + what it received."""
    echo = socket.socket()
    echo.bind(("127.0.0.1", 0))
    echo.listen()

    def run():
        while True:
            try:
                c, _ = echo.accept()
            except OSError:
                return
            data = c.recv(100)
            c.sendall(b"SSH-2.0-echo " + data)
            c.close()
    threading.Thread(target=run, daemon=True).start()
    return echo


def _exit_server(eg):
    loop = asyncio.new_event_loop()
    srv = loop.run_until_complete(asyncio.start_server(eg.handle, "127.0.0.1", 0))
    threading.Thread(target=loop.run_forever, daemon=True).start()
    return srv.sockets[0].getsockname()[1]


def test_end_to_end_through_the_box_proxycommand(eg, monkeypatch):
    """ssh-connect.py → CONNECT → bytes relayed both ways; another port is
    refused. (Loopback is never a real destination, so the policy is patched
    to let the test's local "sshd" through on its port only.)"""
    echo = _echo_server()
    echo_port = echo.getsockname()[1]
    monkeypatch.setattr(eg, "allowed",
                        lambda h, p: None if p == echo_port else "only SSH ports")
    port = _exit_server(eg)
    env = {"AIFORGE_SSH_PROXY": f"127.0.0.1:{port}", "PATH": "/usr/bin:/bin"}
    script = str(REPO / "docker" / "ssh-connect.py")
    out = subprocess.run([sys.executable, script, "127.0.0.1", str(echo_port)],
                         input=b"hello", capture_output=True, timeout=10, env=env)
    assert out.stdout.startswith(b"SSH-2.0-echo hello"), out
    bad = subprocess.run([sys.executable, script, "127.0.0.1", "443"],
                         input=b"x", capture_output=True, timeout=10, env=env)
    assert bad.returncode == 1 and b"refused" in bad.stderr
    echo.close()


def test_only_connect_is_served(eg):
    port = _exit_server(eg)
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n")
    assert s.recv(100).startswith(b"HTTP/1.1 405")
