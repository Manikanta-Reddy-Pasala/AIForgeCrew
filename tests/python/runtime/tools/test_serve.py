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


def test_serve_requires_cmd(srv):
    assert srv.serve({})["error"] == "missing 'cmd'"


def test_serve_detects_port_and_stops(srv):
    r = srv.serve({"cmd": "python3 -m http.server 8781", "port": 8781,
                   "wait_s": 2, "ttl_s": 9999})
    assert r["ok"] and r["url"] == "http://localhost:8781" and r["pid"]
    assert srv.list_services()["services"][0]["alive"]
    assert srv.stop_service({"pid": r["pid"]})["ok"]
    time.sleep(0.4)
    assert not _alive(r["pid"])


def test_serve_crash_on_startup(srv):
    r = srv.serve({"cmd": "exit 7", "wait_s": 2})
    assert r["ok"] is False and "exited on startup" in r["error"]


def test_serve_default_ttl(srv):
    r = srv.serve({"cmd": "python3 -m http.server 8782", "port": 8782, "wait_s": 1})
    assert r["ttl_s"] == 1800.0          # default 30 min
    srv.stop_service({"pid": r["pid"]})


def test_serve_ttl_auto_cleanup(srv):
    r = srv.serve({"cmd": "python3 -m http.server 8783", "port": 8783,
                   "wait_s": 1, "ttl_s": 4})
    pid = r["pid"]
    assert _alive(pid)
    time.sleep(5)
    srv._reap()                          # the reaper thread does this every 60s
    assert not _alive(pid)               # forgotten service auto-killed
    assert srv.list_services()["services"] == []
