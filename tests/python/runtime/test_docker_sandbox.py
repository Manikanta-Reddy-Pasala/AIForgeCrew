from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from aiforge_core.runtime import docker_sandbox as ds


@pytest.fixture(autouse=True)
def _reset():
    ds._containers.clear()
    yield
    ds._containers.clear()


def test_is_enabled_off_when_env_unset(monkeypatch):
    monkeypatch.delenv("AIFORGE_DOCKER_SANDBOX", raising=False)
    assert ds.is_enabled() is False


def test_is_enabled_off_when_docker_missing(monkeypatch):
    monkeypatch.setenv("AIFORGE_DOCKER_SANDBOX", "1")
    monkeypatch.setattr(ds, "_docker_available", lambda: False)
    assert ds.is_enabled() is False


def test_is_enabled_on_when_env_and_docker_ok(monkeypatch):
    monkeypatch.setenv("AIFORGE_DOCKER_SANDBOX", "1")
    monkeypatch.setattr(ds, "_docker_available", lambda: True)
    with patch.object(ds, "subprocess") as mock_sp:
        mock_sp.run.return_value = MagicMock(returncode=0)
        assert ds.is_enabled() is True


def test_exec_happy(monkeypatch):
    monkeypatch.setattr(ds, "_docker_available", lambda: True)
    monkeypatch.setattr(ds, "_container_exists", lambda n: True)
    fake_proc = MagicMock(returncode=0, stdout=b"hi\n", stderr=b"")
    with patch.object(ds.subprocess, "run", return_value=fake_proc):
        out = ds.exec_in_container("rid", "echo hi")
    assert out["ok"]
    assert out["stdout"].strip() == "hi"
    assert out["sandbox"] == "docker"


def test_exec_empty_command(monkeypatch):
    monkeypatch.setattr(ds, "_docker_available", lambda: True)
    monkeypatch.setattr(ds, "_container_exists", lambda n: True)
    out = ds.exec_in_container("rid", "")
    assert out["ok"] is False
    assert out["error"] == "empty_command"


def test_exec_nonzero_exit(monkeypatch):
    monkeypatch.setattr(ds, "_docker_available", lambda: True)
    monkeypatch.setattr(ds, "_container_exists", lambda n: True)
    fake_proc = MagicMock(returncode=1, stdout=b"", stderr=b"boom\n")
    with patch.object(ds.subprocess, "run", return_value=fake_proc):
        out = ds.exec_in_container("rid", "false")
    assert out["ok"] is False
    assert out["returncode"] == 1
    assert "boom" in out["stderr"]


def test_exec_timeout(monkeypatch):
    monkeypatch.setattr(ds, "_docker_available", lambda: True)
    monkeypatch.setattr(ds, "_container_exists", lambda n: True)
    timeout_exc = ds.subprocess.TimeoutExpired(cmd="cmd", timeout=1)
    timeout_exc.stdout = b"x"
    timeout_exc.stderr = b"y"
    with patch.object(ds.subprocess, "run", side_effect=timeout_exc):
        out = ds.exec_in_container("rid", "sleep 5", timeout=1)
    assert out["ok"] is False
    assert out["error"] == "timeout"


def test_container_reuse_across_calls(monkeypatch):
    monkeypatch.setattr(ds, "_docker_available", lambda: True)
    monkeypatch.setattr(ds, "_container_exists", lambda n: True)
    fake_proc = MagicMock(returncode=0, stdout=b"", stderr=b"")
    with patch.object(ds.subprocess, "run", return_value=fake_proc):
        n1 = ds.ensure_container("rid")
        n2 = ds.ensure_container("rid")
    assert n1 == n2 == "aiforge-sandbox-rid"


def test_destroy_idempotent(monkeypatch):
    monkeypatch.setattr(ds, "_docker_available", lambda: True)
    monkeypatch.setattr(ds, "_container_exists", lambda n: False)
    ds.destroy_container("never-existed")  # must not raise
