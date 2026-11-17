"""Handed-off jobs never outlive their server."""
import os
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from aiforge_core.runtime import job_reaper as jr

pytestmark = pytest.mark.skipif(not os.path.isdir("/proc"), reason="Linux /proc")


@pytest.fixture(autouse=True)
def _cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("AIFORGE_JOB_REAPER", raising=False)
    monkeypatch.setattr(jr, "_path", lambda: str(tmp_path / "run" / "jobs.json"))
    monkeypatch.setattr(jr, "_SWEPT", False)


def _spin():
    return subprocess.Popen([sys.executable, "-c", "import time\nwhile True: time.sleep(0.2)"],
                            start_new_session=True)


def test_sweep_kills_a_group_left_by_a_dead_server():
    p = _spin()
    try:
        cmd = " ".join(p.args)
        jr._save({str(p.pid): {"server": 999999, "cmd": cmd}})       # that server is gone
        assert jr.sweep() == 1
        p.wait(timeout=5)
        assert jr._load() == {}
    finally:
        if p.poll() is None:
            p.kill()


def test_sweep_spares_a_live_server_and_a_reused_pid():
    p = _spin()
    try:
        jr._save({str(p.pid): {"server": os.getppid(), "cmd": " ".join(p.args)},   # live server
                  str(os.getpid()): {"server": 999999, "cmd": "cargo test something else"}})
        assert jr.sweep() == 0
        assert p.poll() is None
        assert str(p.pid) in jr._load()
    finally:
        p.kill()


def test_record_forget_and_exit_kill(monkeypatch):
    p = _spin()
    job = SimpleNamespace(pgid=p.pid, explicit=False, cmd=" ".join(p.args), alive=lambda: p.poll() is None)
    try:
        jr.record(job)
        assert str(p.pid) in jr._load()
        from aiforge_core.runtime import cmd_jobs
        monkeypatch.setattr(cmd_jobs, "_JOBS", {"bg-1": job})
        assert jr.kill_on_exit() == 1
        p.wait(timeout=5)
        assert str(p.pid) not in jr._load()
    finally:
        if p.poll() is None:
            p.kill()
    jr.record(SimpleNamespace(pgid=123, explicit=True, cmd="serve"))        # explicit: not tracked
    assert "123" not in jr._load()
