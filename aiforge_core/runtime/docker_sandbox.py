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


def sandbox_policy() -> str:
    """Resolve the sandbox policy from the environment.

    * ``AIFORGE_SANDBOX_REQUIRED=1`` -> ``"required"`` (host fallback
      forbidden; refuse if docker is unavailable).
    * else ``AIFORGE_DOCKER_SANDBOX=1`` -> ``"preferred"`` (use docker
      when reachable, else fall back to host silently).
    * otherwise -> ``"off"``.
    """
    if os.environ.get("AIFORGE_SANDBOX_REQUIRED", "0") in ("1", "true"):
        return "required"
    if os.environ.get("AIFORGE_DOCKER_SANDBOX", "0") in ("1", "true"):
        return "preferred"
    return "off"


def resolve_exec(docker_available: bool) -> dict[str, str]:
    """Decide where a command should run given policy + docker state.

    Returns ``{"mode": "docker"|"host"|"refuse", "reason": ...}``.
    Callers consult this to route execution; a ``"refuse"`` decision
    means the exec must NOT silently run on host.
    """
    policy = sandbox_policy()
    if policy == "required":
        if docker_available:
            return {"mode": "docker", "reason": "policy_required_docker_ok"}
        return {
            "mode": "refuse",
            "reason": (
                "AIFORGE_SANDBOX_REQUIRED=1 but docker is unavailable; "
                "host fallback is forbidden"
            ),
        }
    if policy == "preferred":
        if docker_available:
            return {"mode": "docker", "reason": "policy_preferred_docker_ok"}
        return {"mode": "host", "reason": "policy_preferred_docker_down"}
    return {"mode": "host", "reason": "policy_off"}


def is_enabled() -> bool:
    """``True`` when execution should route through this module.

    Returns ``True`` when docker is opted-in AND reachable, OR when the
    policy is ``required`` (regardless of docker reachability) so that a
    missing/broken docker is forced through :func:`exec_in_container`,
    which refuses rather than silently falling back to host exec.
    """
    policy = sandbox_policy()
    if policy == "off":
        return False
    if policy == "required":
        return True
    # preferred: only route through docker when actually reachable
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
    """Lazy-create the per-run container and return its name.

    Mount mode:
      ``AIFORGE_DOCKER_VOLUME_MODE=ro`` (default, safe) — workspace
      mounted read-only; container writes go to its own filesystem.
      ``AIFORGE_DOCKER_VOLUME_MODE=rw`` — workspace mounted read-write;
      container can edit operator files. ONLY use when ScopeGuard +
      agent allowlist provides sufficient containment.
    """
    if run_id in _containers:
        return _containers[run_id]
    name = _container_name(run_id)
    if not _container_exists(name):
        image = os.environ.get("AIFORGE_DOCKER_IMAGE", _DEFAULT_IMAGE)
        network = os.environ.get("AIFORGE_DOCKER_NETWORK", "bridge")
        memory = os.environ.get("AIFORGE_DOCKER_MEMORY", "1g")
        cpus = os.environ.get("AIFORGE_DOCKER_CPUS", "2")
        vol_mode = os.environ.get(
            "AIFORGE_DOCKER_VOLUME_MODE", "ro",
        ).lower()
        if vol_mode not in ("ro", "rw"):
            vol_mode = "ro"
        repo = str(root())
        subprocess.run(
            [
                "docker", "run", "-d", "--name", name,
                "--network", network,
                "--memory", memory, "--cpus", cpus,
                "-v", f"{repo}:/workspace:{vol_mode}",
                "--workdir", "/workspace",
                image, "tail", "-f", "/dev/null",
            ],
            check=True, capture_output=True,
        )
        emit("DockerSandbox", {"action": "created", "name": name,
                               "image": image, "vol_mode": vol_mode})
    _containers[run_id] = name
    return name


def exec_in_container(
    run_id: str, command: str, *, timeout: int = 90,
) -> dict[str, Any]:
    """Run ``command`` inside the per-run container (``bash -lc``).

    Consults :func:`resolve_exec`. A ``"refuse"`` decision (mandatory
    sandbox + docker unavailable) returns a refusal result instead of
    running on host.
    """
    if not command or not command.strip():
        return {"ok": False, "error": "empty_command"}
    decision = resolve_exec(_docker_available())
    if decision["mode"] == "refuse":
        emit("DockerSandbox", {"action": "refused",
                               "reason": decision["reason"]})
        return {
            "ok": False,
            "error": "sandbox_required",
            "reason": decision["reason"],
            "command": command,
        }
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
    "sandbox_policy",
    "resolve_exec",
    "is_enabled",
    "ensure_container",
    "exec_in_container",
    "destroy_container",
]
