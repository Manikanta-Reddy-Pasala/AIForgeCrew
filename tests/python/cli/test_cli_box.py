"""The sandbox: what the CLI generates, and what it refuses to guess."""

from __future__ import annotations

import pytest
from aiforge_cli import box, mounts
from aiforge_cli.config import Config


def _cfg(tmp_path, repo=None) -> Config:
    return Config(port=8799, config_dir=tmp_path / ".aiforge", repo=repo,
                  image="aiforge-sandbox:local", auto_mount=False, verbosity=0,
                  json_events=False)


def test_the_generated_compose_publishes_loopback_and_mounts_the_config_dir(tmp_path):
    cfg = _cfg(tmp_path)
    text = box.render_compose(cfg, [], env={}, plat="posix")
    assert "image: 'aiforge-sandbox:local'" in text
    # Published port, not host networking: Docker Desktop has no usable host
    # network on macOS or Windows, and the API must answer on 127.0.0.1.
    assert "'127.0.0.1:8799:8799'" in text
    assert f"'{cfg.config_dir}:/home/aiforge/.aiforge'" in text
    assert "aiforge-state:/var/lib/aiforge" in text


def test_only_approved_folders_reach_the_compose_file(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.config_dir.mkdir(parents=True)
    work = tmp_path / "work"
    work.mkdir()
    sneaky = tmp_path / "sneaky"
    sneaky.mkdir()
    # The agent can append to mounts.list; only `work` was approved by the host.
    cfg.mounts_file.write_text(f"{work}\n{sneaky}\n")
    approvals = tmp_path / "cfg" / "aiforge" / "approved-mounts"
    approvals.parent.mkdir(parents=True)
    approvals.write_text(f"{work}\n")
    effective = mounts.effective(cfg.mounts_file, approvals, home=str(tmp_path / "home"))
    text = box.render_compose(cfg, effective, env={}, plat="posix")
    assert str(work) in text
    assert str(sneaky) not in text


def test_windows_mounts_land_under_host_drive_in_single_quotes(tmp_path):
    cfg = _cfg(tmp_path)
    text = box.render_compose(cfg, [r"C:\Users\m\work"], env={}, plat="nt")
    # SINGLE quotes: a double-quoted YAML scalar reads \U as a unicode escape,
    # so every Windows path made the compose file unparseable.
    assert r"'C:\Users\m\work:/host/c/Users/m/work'" in text
    assert r'"C:\Users' not in text


def test_the_box_mount_list_is_always_colon_joined(tmp_path):
    # The reader is inside the box (sandbox_mounts splits on ":"), and these
    # are box paths, where a Windows drive colon is already /host/<drive>.
    cfg = _cfg(tmp_path)
    nt = box.render_compose(cfg, [r"C:\a", r"D:\b"], env={}, plat="nt")
    line = next(li for li in nt.splitlines() if "AIFORGE_MOUNTS" in li)
    assert "/host/c/a:/host/d/b" in line
    assert ";" not in line


def test_the_compose_file_is_not_written_where_the_box_can_read_it(tmp_path,
                                                                   monkeypatch):
    cfg = _cfg(tmp_path)
    env = {"XDG_CONFIG_HOME": str(tmp_path / "cfg")}
    path = box.compose_path(env)
    # ~/.aiforge is mounted INTO the box; the compose file lists every host
    # mount and every passthrough env value, proxy credentials included.
    assert str(cfg.config_dir) not in str(path)
    assert "cfg" in str(path)


def test_only_set_passthrough_variables_are_written(tmp_path):
    cfg = _cfg(tmp_path)
    text = box.render_compose(cfg, [], env={"AIFORGE_LM_BASE_URL": "http://ms:1234/v1"},
                              plat="posix")
    assert "AIFORGE_LM_BASE_URL=http://ms:1234/v1" in text
    assert "HTTPS_PROXY" not in text


def test_waiting_for_health_gives_up_with_the_command_that_explains_why(tmp_path):
    ticks = iter(range(0, 400))
    with pytest.raises(box.BoxError) as exc:
        box.wait_healthy(lambda: False, timeout=5.0, interval=1.0,
                         clock=lambda: next(ticks), sleep=lambda _s: None)
    assert "box logs" in str(exc.value)


def test_a_healthy_api_returns_as_soon_as_it_answers(tmp_path):
    ticks = iter([0.0, 0.5, 1.5])
    answers = iter([False, True])
    waited = box.wait_healthy(lambda: next(answers), interval=0.1,
                              clock=lambda: next(ticks), sleep=lambda _s: None)
    assert waited == 1.5          # the clock moved: it really did wait a round


def test_a_missing_image_and_no_repo_names_all_three_ways_out(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(box, "require_docker", lambda **kw: "/usr/bin/docker")
    monkeypatch.setattr(box, "require_compose", lambda exe: None)
    monkeypatch.setattr(box, "daemon_kind", lambda exe, env=None: "native")
    monkeypatch.setattr(box, "_refuse_a_foreign_box", lambda exe, env: None)
    monkeypatch.setattr(box, "image_present", lambda _exe, _image: False)
    with pytest.raises(box.BoxError) as exc:
        box.start(cfg, env={})
    message = str(exc.value)
    assert "docker pull" in message
    assert "AIFORGE_SANDBOX_IMAGE" in message
    assert "run.sh" in message


def test_one_sandbox_is_shared_the_second_starter_waits(tmp_path):
    # Every connection uses ONE box. Two cold starts at once both ran
    # `compose up`, and the loser got "container name already in use".
    env = {"XDG_CONFIG_HOME": str(tmp_path / "cfg")}
    with box.start_lock(env) as first:
        assert first is True
        with box.start_lock(env) as second:
            assert second is False        # waits for the holder instead
    with box.start_lock(env) as again:
        assert again is True              # released when the holder is done


def test_the_start_lock_lives_outside_the_mounted_config_dir(tmp_path):
    env = {"XDG_CONFIG_HOME": str(tmp_path / "cfg")}
    assert ".aiforge" not in str(box.start_lock_path(env))


# ── a stopped docker daemon is started, not just reported ─────────────────

def _docker_fakes(monkeypatch, *, up_after: int, launched: list, context: str = "default"):
    """`docker info` fails until it has been asked ``up_after`` times."""
    import subprocess as sp
    calls = {"info": 0}

    def fake_run(argv, **kw):
        if argv[1:2] == ["info"]:
            calls["info"] += 1
            ok = calls["info"] > up_after
            return sp.CompletedProcess(argv, 0 if ok else 1, "27.0", "" if ok else "Cannot connect")
        if argv[1:3] == ["context", "show"]:
            return sp.CompletedProcess(argv, 0, context + "\n", "")
        launched.append(argv)
        return sp.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(box, "docker_bin", lambda: "/usr/bin/docker")
    monkeypatch.setattr(box.subprocess, "run", fake_run)
    return calls


class _Clock:
    """Time that moves only when the code sleeps — and can be made slow, like a
    `docker info` that hangs against a half-started engine."""
    def __init__(self, per_check: float = 0.0):
        self.t, self.per_check = 0.0, per_check

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s + self.per_check


def test_a_stopped_docker_is_started_and_waited_for(monkeypatch):
    launched: list = []
    _docker_fakes(monkeypatch, up_after=3, launched=launched)
    monkeypatch.setattr(box._platform, "system", lambda: "Darwin")
    clock, waits = _Clock(), []
    assert box.require_docker(launch=True, on_wait=waits.append, sleep=clock.sleep,
                              clock=clock, env={}) == "/usr/bin/docker"
    assert launched == [["open", "-g", "-a", "Docker"]]
    assert waits == [2.0, 4.0, 6.0]              # up on the 4th `docker info`


def test_linux_starts_the_user_service_without_blocking_or_sudo(monkeypatch):
    launched: list = []
    _docker_fakes(monkeypatch, up_after=1, launched=launched)
    monkeypatch.setattr(box._platform, "system", lambda: "Linux")
    clock = _Clock()
    box.require_docker(launch=True, sleep=clock.sleep, clock=clock, env={})
    assert launched == [["systemctl", "--user", "--no-block", "start", "docker"]]


def test_the_wait_is_by_the_clock_not_by_counting_sleeps(monkeypatch):
    launched: list = []
    _docker_fakes(monkeypatch, up_after=10_000, launched=launched)
    monkeypatch.setattr(box._platform, "system", lambda: "Darwin")
    clock = _Clock(per_check=25.0)               # every check hangs 25 s
    with pytest.raises(box.BoxError, match="did not answer within 120s"):
        box.require_docker(launch=True, sleep=clock.sleep, clock=clock, env={})
    assert clock.t < box.DOCKER_START_WAIT_S + 30  # ~2 min, not 60 checks x 27 s


def test_no_launch_for_status_or_for_a_daemon_that_is_not_local(monkeypatch):
    launched: list = []
    _docker_fakes(monkeypatch, up_after=10_000, launched=launched)
    monkeypatch.setattr(box._platform, "system", lambda: "Darwin")
    with pytest.raises(box.BoxError, match="Start Docker Desktop"):
        box.require_docker()                     # no launch: status/logs never start docker
    with pytest.raises(box.BoxError, match=r"DOCKER_HOST=ssh://nuc is not answering"):
        box.require_docker(launch=True, env={"DOCKER_HOST": "ssh://nuc"})
    _docker_fakes(monkeypatch, up_after=10_000, launched=launched, context="colima")
    with pytest.raises(box.BoxError, match="context `colima`"):
        box.require_docker(launch=True, env={})
    assert launched == []


@pytest.mark.parametrize("host,local", [
    ("unix:///run/user/1000/docker.sock", True),          # rootless, as Docker's docs export it
    ("unix:///Users/me/.docker/run/docker.sock", True),   # Docker Desktop on macOS
    ("npipe:////./pipe/docker_engine", True),
    ("unix:///Users/me/.colima/default/docker.sock", False),
    ("tcp://10.0.0.5:2376", False),
    ("ssh://nuc", False),
])
def test_a_local_docker_host_socket_still_gets_the_auto_start(host, local):
    assert box._host_is_local(host) is local


def test_docker_desktop_for_linux_starts_its_own_unit(monkeypatch):
    launched: list = []
    _docker_fakes(monkeypatch, up_after=1, launched=launched, context="desktop-linux")
    monkeypatch.setattr(box._platform, "system", lambda: "Linux")
    clock = _Clock()
    box.require_docker(launch=True, sleep=clock.sleep, clock=clock, env={})
    assert launched == [["systemctl", "--user", "--no-block", "start", "docker-desktop"]]


# ── waiting for a first start ─────────────────────────────────────────────


def _wait(monkeypatch, statuses, *, logs=lambda *a, **k: "installing deps"):
    """Drive wait_ready with a fake clock: each probe (every 5 s) reads the
    next (state, restarts); the API never answers."""
    seq = iter(statuses)
    monkeypatch.setattr(box, "docker_bin", lambda: "docker")
    monkeypatch.setattr(box, "container_status", lambda exe, env=None: next(seq))
    monkeypatch.setattr(box, "log_tail", logs)
    now = [0.0]
    return box.wait_ready(lambda: False, env={}, timeout=600,
                          clock=lambda: now[0],
                          sleep=lambda s: now.__setitem__(0, now[0] + 5.0))


def test_a_crash_loop_sampled_as_running_is_caught_by_its_restart_count(monkeypatch):
    with pytest.raises(box.BoxError, match="keeps restarting"):
        _wait(monkeypatch, [("running", 0), ("running", 1), ("running", 2)])


def test_a_paused_box_fails_at_once(monkeypatch):
    with pytest.raises(box.BoxError, match="paused"):
        _wait(monkeypatch, [("paused", 0)])


def test_no_container_at_all_fails_after_a_minute_not_thirty(monkeypatch):
    with pytest.raises(box.BoxError, match="no container named"):
        _wait(monkeypatch, [("missing", 0)] * 20)


def test_a_docker_that_times_out_is_unknown_not_a_traceback(monkeypatch):
    def slow_logs(*a, **k):
        raise box.subprocess.TimeoutExpired("docker logs", 10)
    monkeypatch.setattr(box.subprocess, "run", lambda *a, **k: slow_logs())
    assert box.log_tail("docker") == ""
    assert box.container_status("docker") == ("unknown", 0)


def test_the_binary_uses_its_own_source_even_inside_a_checkout(tmp_path, monkeypatch):
    """Otherwise one machine got two boxes fighting over one container name."""
    from aiforge_cli import config, payload
    repo = tmp_path / "AIForgeCrew"
    for marker in config._MARKERS:
        (repo / marker).parent.mkdir(parents=True, exist_ok=True)
        (repo / marker).write_text("")
    monkeypatch.setattr(payload, "tarball", lambda: None)
    assert config._find_repo({}, repo) == repo                     # from source: run.sh
    monkeypatch.setattr(payload, "tarball", lambda: tmp_path / "sandbox-src.tar.gz")
    assert config._find_repo({}, repo) is None                     # the binary: its own
    assert config._find_repo({"AIFORGE_REPO": str(repo)}, tmp_path) == repo


def test_a_first_probe_that_timed_out_is_not_a_restart_baseline(monkeypatch):
    """"unknown" reports 0 restarts: an old box with restarts in its history
    must not then look like a crash loop."""
    assert _wait_until_healthy(monkeypatch, [("unknown", 0), ("running", 5), ("running", 5)])


def _wait_until_healthy(monkeypatch, statuses):
    seq = iter(statuses)
    answers = iter([False] * len(statuses) + [True])
    monkeypatch.setattr(box, "docker_bin", lambda: "docker")
    monkeypatch.setattr(box, "container_status", lambda exe, env=None: next(seq))
    monkeypatch.setattr(box, "log_tail", lambda *a, **k: "")
    now = [0.0]
    box.wait_ready(lambda: next(answers), env={}, timeout=600, clock=lambda: now[0],
                   sleep=lambda s: now.__setitem__(0, now[0] + 5.0))
    return True


def test_a_container_from_a_checkout_is_named_not_collided_with(monkeypatch):
    monkeypatch.setattr(box.subprocess, "run", lambda *a, **k: type(
        "R", (), {"returncode": 0, "stdout": "aiforgecrew\n"})())
    with pytest.raises(box.BoxError, match="started from a checkout"):
        box._refuse_a_foreign_box("docker", {})
    monkeypatch.setattr(box.subprocess, "run", lambda *a, **k: type(
        "R", (), {"returncode": 0, "stdout": "aiforge\n"})())
    box._refuse_a_foreign_box("docker", {})                       # our own: fine


def test_stop_does_not_claim_a_box_it_could_not_see(monkeypatch, tmp_path):
    monkeypatch.setattr(box, "require_docker", lambda **kw: "docker")
    cfg = _cfg(tmp_path)
    for state, stopped in (("exited", True), ("missing", True), ("unknown", False),
                           ("restarting", False), ("running", False)):
        monkeypatch.setattr(box, "container_state", lambda exe, env=None, s=state: s)
        assert box.stop(cfg, {"XDG_CONFIG_HOME": str(tmp_path)}) is stopped, state
