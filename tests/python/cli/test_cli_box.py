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
    assert "image: aiforge-sandbox:local" in text
    # Published port, not host networking: Docker Desktop has no usable host
    # network on macOS or Windows, and the API must answer on 127.0.0.1.
    assert '"127.0.0.1:8799:8799"' in text
    assert f'"{cfg.config_dir}:/home/aiforge/.aiforge"' in text
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
    approvals = tmp_path / "approved"
    approvals.write_text(f"{work}\n")
    effective = mounts.effective(cfg.mounts_file, approvals, home=str(tmp_path / "home"))
    text = box.render_compose(cfg, effective, env={}, plat="posix")
    assert str(work) in text
    assert str(sneaky) not in text


def test_windows_mounts_land_under_host_drive(tmp_path):
    cfg = _cfg(tmp_path)
    text = box.render_compose(cfg, [r"C:\Users\m\work"], env={}, plat="nt")
    assert r'"C:\Users\m\work:/host/c/Users/m/work"' in text


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


def test_a_healthy_api_returns_immediately(tmp_path):
    waited = box.wait_healthy(lambda: True, clock=lambda: 7.0, sleep=lambda _s: None)
    assert waited == 0.0


def test_a_missing_image_and_no_repo_names_all_three_ways_out(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(box, "require_docker", lambda: "/usr/bin/docker")
    monkeypatch.setattr(box, "image_present", lambda _exe, _image: False)
    with pytest.raises(box.BoxError) as exc:
        box.start(cfg, env={})
    message = str(exc.value)
    assert "docker pull" in message
    assert "AIFORGE_SANDBOX_IMAGE" in message
    assert "run.sh" in message
