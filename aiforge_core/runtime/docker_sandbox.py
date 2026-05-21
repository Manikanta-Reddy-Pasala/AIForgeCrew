"""Optional Docker sandbox for ``bash`` + ``execute_ipython_cell`` (sub #7).

Pure-CLI driver (``docker run`` / ``docker exec``) so we don't pull in the
Python SDK. Opt-in: set ``AIFORGE_DOCKER_SANDBOX=1`` and have ``docker``
in PATH. Falls back to host exec silently otherwise — caller treats this
as transparent.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any

from .sandbox import root
from .tools._trace import emit

_DEFAULT_IMAGE = "python:3.12-slim"
_STDOUT_CAP_BYTES = 8000

_containers: dict[str, str] = {}


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def is_enabled() -> bool:
    """``True`` when the operator opted in AND docker is reachable."""
    if os.environ.get("AIFORGE_DOCKER_SANDBOX", "0") not in ("1", "true"):
        return False
    if not _docker_available():
        return False
    probe = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        capture_output=True, timeout=5,
    )
    return probe.returncode == 0


def _container_name(run_id: str) -> str:
    return f"aiforge-sandbox-{run_id}"


def _container_exists(name: str) -> bool:
    proc = subprocess.run(
        ["docker", "inspect", "--type=container", "-f", "{{.State.Running}}", name],
        capture_output=True,
    )
    return proc.returncode == 0


def ensure_container(run_id: str) -> str:
    """Lazy-create the per-run container and return its name."""
    if run_id in _containers:
        return _containers[run_id]
    name = _container_name(run_id)
    if not _container_exists(name):
        image = os.environ.get("AIFORGE_DOCKER_IMAGE", _DEFAULT_IMAGE)
        network = os.environ.get("AIFORGE_DOCKER_NETWORK", "bridge")
        memory = os.environ.get("AIFORGE_DOCKER_MEMORY", "1g")
        cpus = os.environ.get("AIFORGE_DOCKER_CPUS", "2")
        repo = str(root())
        subprocess.run(
            [
                "docker", "run", "-d", "--name", name,
                "--network", network,
                "--memory", memory, "--cpus", cpus,
                "-v", f"{repo}:/workspace:ro",
                "--workdir", "/workspace",
                image, "tail", "-f", "/dev/null",
            ],
            check=True, capture_output=True,
        )
        emit("DockerSandbox", {"action": "created", "name": name,
                               "image": image})
    _containers[run_id] = name
    return name


def exec_in_container(
    run_id: str, command: str, *, timeout: int = 90,
) -> dict[str, Any]:
    """Run ``command`` inside the per-run container (``bash -lc``)."""
    if not command or not command.strip():
        return {"ok": False, "error": "empty_command"}
    name = ensure_container(run_id)
    try:
        proc = subprocess.run(
            ["docker", "exec", "-i", name, "bash", "-lc", command],
            capture_output=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False, "error": "timeout", "command": command,
            "stdout": (exc.stdout or b"").decode("utf-8", "replace")[
                :_STDOUT_CAP_BYTES
            ],
            "stderr": (exc.stderr or b"").decode("utf-8", "replace")[
                :_STDOUT_CAP_BYTES
            ],
            "truncated": True,
        }
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "command": command,
        "stdout": out[:_STDOUT_CAP_BYTES],
        "stderr": err[:_STDOUT_CAP_BYTES],
        "truncated": (
            len(out) > _STDOUT_CAP_BYTES or len(err) > _STDOUT_CAP_BYTES
        ),
        "sandbox": "docker",
    }


def destroy_container(run_id: str) -> None:
    """Stop + remove the per-run container (best-effort, idempotent)."""
    name = _containers.pop(run_id, _container_name(run_id))
    if not _docker_available():
        return
    if _container_exists(name):
        subprocess.run(
            ["docker", "rm", "-f", name], capture_output=True,
        )
        emit("DockerSandbox", {"action": "destroyed", "name": name})


__all__ = [
    "is_enabled",
    "ensure_container",
    "exec_in_container",
    "destroy_container",
]
