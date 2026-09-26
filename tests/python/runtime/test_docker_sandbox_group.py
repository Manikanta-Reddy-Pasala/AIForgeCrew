"""The docker sandbox measures and stops the command INSIDE its container
(runtime/docker_group), is checked on like every shell path, answers Stop and
honours AIFORGE_SHELL_TIMEOUT. Docker is faked at its exec layer; a real
docker run is at the end (skipped where there is no docker)."""
from __future__ import annotations

import shutil
import subprocess
import time
import types

import pytest

from aiforge_core.runtime import cmd_idle, cmd_jobs, docker_group, proc_signals
from aiforge_core.runtime import docker_sandbox as ds


class _FakeDocker:
    """The container side: ``stat`` answers from ``cpu`` (seconds), every
    signal is recorded."""

    def __init__(self, cpu=lambda: 0.0) -> None:
        self.cpu = cpu
        self.signals: list[str] = []
        self.calls: list[list[str]] = []

    def __call__(self, args, timeout=30.0):
        self.calls.append(list(args))
        what = args[-2]
        if what == "stat":
            ticks = int(self.cpu() * 100)
            line = f"42 (bash) S 1 42 42 0 -1 0 0 0 0 0 {ticks} 0 0 0 20 0 1"
            return types.SimpleNamespace(returncode=0, stdout=line.encode())
        self.signals.append(what)
        return types.SimpleNamespace(returncode=0, stdout=b"")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    docker_group._reset_for_tests()
    ds._containers.clear()
    monkeypatch.setattr(docker_group, "_tick", lambda: 100.0)
    monkeypatch.setattr(ds, "_docker_available", lambda: True)
    monkeypatch.setattr(ds, "_container_exists", lambda n: True)
    monkeypatch.delenv("AIFORGE_SHELL_TIMEOUT", raising=False)
    monkeypatch.delenv("AIFORGE_DOCKER_SANDBOX", raising=False)
    monkeypatch.delenv("AIFORGE_SANDBOX_REQUIRED", raising=False)
    yield
    docker_group._reset_for_tests()
    ds._containers.clear()


def _local(monkeypatch, script: str) -> None:
    """Run ``script`` on the host in place of ``docker exec`` (the fake exec
    layer): the host process is as idle as a real docker CLI would be."""
    monkeypatch.setattr(docker_group.RemoteGroup, "argv",
                        lambda self, command: ["bash", "-c", script])


# ── the exec layer ─────────────────────────────────────────────────────────

def test_the_command_is_marked_so_its_container_processes_can_be_found():
    g = docker_group.RemoteGroup("box", key="k1")
    argv = g.argv("make")
    assert argv[:2] == ["docker", "exec"] and "-e" in argv
    assert argv[argv.index("-e") + 1] == "AIFORGE_JOB=k1"
    assert argv[-3:] == ["bash", "-lc", "make"]


def test_cpu_is_read_inside_the_container(monkeypatch):
    fake = _FakeDocker(cpu=lambda: 12.5)
    monkeypatch.setattr(docker_group, "_docker", fake)
    g = docker_group.RemoteGroup("box", key="k2")
    assert g.cpu_s() == pytest.approx(12.5)
    assert fake.calls[0][:2] == ["exec", "box"] and fake.calls[0][-1] == "k2"


def test_the_idle_detector_and_every_stop_path_reach_the_container(monkeypatch):
    """group_cpu_s / kill_group on the docker CLI's host group act on the
    command in the container — so run_to_completion, the job table, the
    background watcher and chat Stop all do."""
    fake = _FakeDocker(cpu=lambda: 3.0)
    monkeypatch.setattr(docker_group, "_docker", fake)
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        docker_group.register(proc.pid, docker_group.RemoteGroup("box"))
        assert cmd_idle.group_cpu_s(proc.pid) == pytest.approx(3.0)
        proc_signals.stop_group(proc.pid, pause_s=0.0)
        assert fake.signals[:2] == ["TERM", "KILL"]
    finally:
        proc.kill()
        proc.wait()


# ── the sandbox command, checked on ────────────────────────────────────────

def test_a_silent_cpu_bound_build_is_not_killed_as_hung(monkeypatch):
    monkeypatch.setenv("AIFORGE_CMD_IDLE_S", "0.4")
    monkeypatch.setenv("AIFORGE_CMD_CHECKIN_S", "0")
    burned = {"s": 0.0}

    def cpu():
        burned["s"] += 1.0             # the compiler, busy in the container
        return burned["s"]
    fake = _FakeDocker(cpu=cpu)
    monkeypatch.setattr(docker_group, "_docker", fake)
    _local(monkeypatch, "sleep 1.5; echo built")
    out = ds.exec_in_container("rid", "make")
    assert out["ok"], out
    assert "built" in out["stdout"] and out["sandbox"] == "docker"
    assert not fake.signals


def test_a_really_hung_command_is_killed_in_the_container(monkeypatch):
    monkeypatch.setenv("AIFORGE_CMD_IDLE_S", "0.4")
    monkeypatch.setenv("AIFORGE_CMD_CHECKIN_S", "0")
    fake = _FakeDocker(cpu=lambda: 0.0)
    monkeypatch.setattr(docker_group, "_docker", fake)
    _local(monkeypatch, "sleep 30")
    t0 = time.monotonic()
    out = ds.exec_in_container("rid", "read x")
    assert time.monotonic() - t0 < 15
    assert out["ok"] is False and "hung" in out["error"]
    assert "TERM" in fake.signals and "KILL" in fake.signals


def test_stop_ends_a_sandbox_command(monkeypatch):
    from aiforge_core.runtime import chat_cancel, run_interrupt
    monkeypatch.setenv("AIFORGE_CMD_CHECKIN_S", "0")
    fake = _FakeDocker(cpu=lambda: 1.0)
    monkeypatch.setattr(docker_group, "_docker", fake)
    monkeypatch.setattr(chat_cancel, "active", lambda: 77)
    started = time.monotonic()
    monkeypatch.setattr(run_interrupt, "attention",
                        lambda sid, only_replace=False:
                        "stop" if time.monotonic() - started > 0.5 else None)
    _local(monkeypatch, "sleep 30")
    out = ds.exec_in_container("rid", "npm run dev")
    assert out.get("stopped") is True and out["error"] == "stopped by user"
    assert "TERM" in fake.signals


def test_a_printing_command_comes_back_as_a_job(monkeypatch):
    """A watch / dev server that keeps printing does not block the tool:
    it is handed back at the check-in, and command_kill reaches the
    container."""
    monkeypatch.setenv("AIFORGE_CMD_CHECKIN_S", "0.5")
    fake = _FakeDocker(cpu=lambda: 1.0)
    monkeypatch.setattr(docker_group, "_docker", fake)
    _local(monkeypatch, "while true; do echo tick; sleep 0.1; done")
    turn = cmd_jobs.begin_turn()
    try:
        out = ds.exec_in_container("rid", "npm run watch")
        assert out.get("running") is True and out["sandbox"] == "docker"
        job = cmd_jobs.find(out["id"])
        assert job is not None
        assert docker_group.lookup(job.pgid) is not None
        job.kill()
        assert "TERM" in fake.signals or "KILL" in fake.signals
    finally:
        cmd_jobs.end_turn(turn)


def test_the_shell_timeout_knob_is_honoured(monkeypatch):
    monkeypatch.setenv("AIFORGE_SHELL_TIMEOUT", "0.5")
    monkeypatch.setenv("AIFORGE_CMD_CHECKIN_S", "0")
    fake = _FakeDocker(cpu=lambda: time.monotonic())
    monkeypatch.setattr(docker_group, "_docker", fake)
    _local(monkeypatch, "sleep 30")
    t0 = time.monotonic()
    out = ds.exec_in_container("rid", "make")
    assert time.monotonic() - t0 < 15
    assert out["ok"] is False and out["error"] == "timeout"
    assert "TERM" in fake.signals


def test_a_finished_command_leaves_no_registry_entry(monkeypatch):
    monkeypatch.setenv("AIFORGE_CMD_CHECKIN_S", "0")
    monkeypatch.setattr(docker_group, "_docker", _FakeDocker())
    _local(monkeypatch, "echo ok")
    assert ds.exec_in_container("rid", "echo ok")["ok"]
    assert docker_group._GROUPS == {}


# ── a real container, where docker is available ───────────────────────────

def _docker_up() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True,
                              timeout=10).returncode == 0
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _docker_up(), reason="no docker on this box")
def test_real_docker_cpu_and_kill_reach_the_container():
    name = f"aiforge-grp-test-{int(time.time())}"
    subprocess.run(["docker", "run", "-d", "--name", name, "busybox",
                    "tail", "-f", "/dev/null"], check=True,
                   capture_output=True)
    try:
        g = docker_group.RemoteGroup(name)
        cli = subprocess.Popen(
            g.argv("i=0; while :; do i=$((i+1)); done")[:-3]
            + ["sh", "-c", "i=0; while :; do i=$((i+1)); done"],
            start_new_session=True)
        time.sleep(2)
        assert (g.cpu_s() or 0) > 0.5
        g.stop(pause_s=0.2)
        time.sleep(0.5)
        assert g.cpu_s() is None
        cli.kill()
        cli.wait()
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
