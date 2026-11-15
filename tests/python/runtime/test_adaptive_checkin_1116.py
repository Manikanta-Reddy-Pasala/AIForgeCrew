"""A command that took d seconds last time waits ~1.3x d in the foreground
before it becomes a job, so a 30-s test run comes back in one call.

Live case: a real PosClientBackend job spent 11 of 76 model calls on
command_wait — each ~30-s Maven run went to the background at 15 s.
"""
from types import SimpleNamespace

from aiforge_core.runtime import cmd_finished, cmd_jobs


def setup_function():
    cmd_jobs._DURATIONS.clear()


def test_unknown_command_uses_the_base(monkeypatch):
    monkeypatch.delenv("AIFORGE_CMD_CHECKIN_S", raising=False)
    assert cmd_jobs.checkin_s("mvn -o test") == 15.0
    assert cmd_jobs.checkin_s() == 15.0


def test_known_command_waits_for_its_usual_time(monkeypatch):
    monkeypatch.delenv("AIFORGE_CMD_CHECKIN_S", raising=False)
    cmd_jobs.note_duration("cd /r && mvn -o -q test -Dtest=X 2>&1 | tail -40", 30.0)
    # the display pipe is not part of the key
    assert cmd_jobs.checkin_s("cd /r && mvn -o -q test -Dtest=X") == 41.0
    cmd_jobs.note_duration("make all", 900.0)
    assert cmd_jobs.checkin_s("make all") == 180.0          # capped
    cmd_jobs.note_duration("ls", 2.0)
    assert cmd_jobs.checkin_s("ls") == 15.0                  # never below the base


def test_a_finished_job_records_its_time(monkeypatch):
    import time
    job = SimpleNamespace(key="bg-1", cmd="cargo test", streams=[], started=time.monotonic() - 25,
                          killed=None)
    monkeypatch.setattr(cmd_finished, "_whole_tail", lambda s: "")
    cmd_finished.record_job(job, 0, None)
    assert 25 <= cmd_jobs._DURATIONS["cargo test"] < 27
    killed = SimpleNamespace(**{**job.__dict__, "cmd": "slow", "killed": "user"})
    cmd_finished.record_job(killed, -9, "user")
    assert "slow" not in cmd_jobs._DURATIONS


def test_explicit_setting_is_used_as_given(monkeypatch):
    cmd_jobs.note_duration("mvn test", 60.0)
    monkeypatch.setenv("AIFORGE_CMD_CHECKIN_S", "1")
    assert cmd_jobs.checkin_s("mvn test") == 1.0
