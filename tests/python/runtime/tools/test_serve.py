"""Background serve tool — start/stop/list + TTL auto-cleanup."""
import os
import time

import pytest


@pytest.fixture
def srv(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    from aiforge_core.runtime.tools import serve
    yield serve
    serve._stop_all_on_exit()        # clean any leftovers


def _alive(pid):
    try:
        os.kill(pid, 0); return True
    except OSError:
        return False


@pytest.fixture
def port():
    """A port nobody is on RIGHT NOW.

    These tests used to hardcode 8781/8782/8783. One leaked ``http.server``
    (a run killed mid-test, a reaper that didn't get there) then squatted the
    port and every later run failed at the first assert — a permanently red
    test with nothing wrong in the product. Asking the OS for a free port makes
    a leak cost one run instead of all of them.
    """
    import socket
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def test_serve_requires_cmd(srv):
    assert srv.serve({})["error"] == "missing 'cmd'"


def test_serve_detects_port_and_stops(srv, port):
    r = srv.serve({"cmd": f"python3 -m http.server {port}", "port": port,
                   "wait_s": 2, "ttl_s": 9999})
    assert r["ok"] and r["url"] == f"http://localhost:{port}" and r["pid"]
    assert srv.list_services()["services"][0]["alive"]
    assert srv.stop_service({"pid": r["pid"]})["ok"]
    time.sleep(0.4)
    assert not _alive(r["pid"])


def test_serve_crash_on_startup(srv):
    r = srv.serve({"cmd": "exit 7", "wait_s": 2})
    assert r["ok"] is False and "exited on startup" in r["error"]


def test_serve_default_ttl(srv, port):
    r = srv.serve({"cmd": f"python3 -m http.server {port}", "port": port,
                   "wait_s": 1})
    assert r["ttl_s"] == 1800.0          # default 30 min
    srv.stop_service({"pid": r["pid"]})


def test_serve_ttl_auto_cleanup(srv, port):
    r = srv.serve({"cmd": f"python3 -m http.server {port}", "port": port,
                   "wait_s": 1, "ttl_s": 4})
    pid = r["pid"]
    assert _alive(pid)
    time.sleep(5)
    srv._reap()                          # the reaper thread does this every 60s
    assert not _alive(pid)               # forgotten service auto-killed
    assert srv.list_services()["services"] == []


# ── item F: serve routes its cmd through the destructive-delete gate ──────────
def test_serve_refuses_destructive_cmd(srv, monkeypatch):
    monkeypatch.delenv("AIFORGE_CHAT_ALLOW_DELETE", raising=False)
    monkeypatch.delenv("AIFORGE_ALLOW_DELETE", raising=False)
    r = srv.serve({"cmd": "rm -rf build && npm run dev"})
    assert r["ok"] is False and "deletes files" in r["error"]
    # nothing was launched.
    assert srv.list_services()["services"] == []


def test_serve_destructive_allowed_with_confirm(srv):
    # confirm_delete=true (the human's Approve) lets it through the guard; use a
    # benign command that simply exits so we don't actually delete anything.
    r = srv.serve({"cmd": "rm -rf /nonexistent/aiforge-test-xyz; exit 0",
                   "confirm_delete": True, "wait_s": 1})
    # Passed the gate (it tried to start — exited on startup, not refused).
    assert "deletes files" not in (r.get("error") or "")


def test_serve_destructive_allowed_with_env(srv, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_ALLOW_DELETE", "1")
    r = srv.serve({"cmd": "rm -rf /nonexistent/aiforge-test-xyz; exit 0",
                   "wait_s": 1})
    assert "deletes files" not in (r.get("error") or "")


def test_serve_normal_cmd_passes_gate(srv):
    r = srv.serve({"cmd": "python3 -m http.server 8784", "port": 8784,
                   "wait_s": 1, "ttl_s": 9999})
    assert r["ok"] is True
    srv.stop_service({"pid": r["pid"]})
