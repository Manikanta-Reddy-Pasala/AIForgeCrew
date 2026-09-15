"""Starting the sandbox, and knowing why it will not start.

Two strategies, picked by what the host has:

* **repo** — a checkout is present, so ``run.sh`` owns the box. It builds the
  image, generates the mount overlay and knows every env passthrough; the CLI
  would only reimplement it worse.
* **compose** — the normal case for someone who installed a binary: no repo, no
  bash, no python. The CLI writes its own compose file from the template below
  against a prebuilt image and drives ``docker compose`` directly.

Either way the CLI never mounts a folder the host has not approved (see
mounts.py), and never builds an image behind the user's back.
"""

from __future__ import annotations

import contextlib
import os
import platform as _platform
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from . import mounts as mountlist
from . import paths
from .config import Config, approvals_file

PROJECT = "aiforge"          # docker compose -p
SERVICE = "aiforge"
CONTAINER = "aiforge"

# Environment the box needs and the host may have set — the same list
# docker-compose.yml passes through. Anything unset is simply omitted.
PASSTHROUGH = (
    "AIFORGE_LM_BASE_URL", "AIFORGE_ROLE", "AIFORGE_ADMIN_URL", "AIFORGE_SYNC_GROUP",
    "AIFORGE_EXTRAS", "AIFORGE_EMBED_BACKEND", "AIFORGE_RUNNER_POLL_SEC",
    "AIFORGE_NPM_REGISTRY", "AIFORGE_APT_MIRROR", "UV_DEFAULT_INDEX",
    "npm_config_registry", "http_proxy", "https_proxy", "no_proxy",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
)


class BoxError(Exception):
    """Something about this host stops the sandbox from running. The message is
    the fix, not the diagnosis — it is printed verbatim and the CLI exits 3."""


# ── docker ────────────────────────────────────────────────────────────────


def docker_bin() -> str | None:
    return shutil.which("docker")


def _install_hint() -> str:
    sysname = _platform.system()
    if sysname == "Darwin":
        return ("docker is not installed. Install Docker Desktop "
                "(https://docs.docker.com/desktop/install/mac-install/), then re-run aiforge.")
    if sysname == "Windows":
        return ("docker is not installed. Install Docker Desktop "
                "(https://docs.docker.com/desktop/install/windows-install/), "
                "then re-run aiforge.")
    return ("docker is not installed. On Ubuntu/Debian: "
            "sudo apt install docker.io && sudo usermod -aG docker $USER "
            "(log out and back in), then re-run aiforge.")


def _daemon_hint() -> str:
    sysname = _platform.system()
    if sysname == "Darwin":
        return "the docker daemon is not running. Start Docker Desktop, then re-run aiforge."
    if sysname == "Windows":
        return "the docker daemon is not running. Start Docker Desktop, then re-run aiforge."
    return ("the docker daemon is not reachable. Try: sudo systemctl start docker "
            "(or `systemctl --user start docker` for rootless), then re-run aiforge.")


def require_docker() -> str:
    """The docker binary, or a BoxError whose message is the remedy."""
    exe = docker_bin()
    if not exe:
        raise BoxError(_install_hint())
    try:
        r = subprocess.run([exe, "info", "--format", "{{.ServerVersion}}"],
                           capture_output=True, text=True, timeout=25)
    except (OSError, subprocess.SubprocessError) as exc:
        raise BoxError(f"{_daemon_hint()} ({exc})") from exc
    if r.returncode != 0:
        raise BoxError(_daemon_hint())
    return exe


def image_present(exe: str, image: str) -> bool:
    r = subprocess.run([exe, "image", "inspect", image], capture_output=True, text=True)
    return r.returncode == 0


def container_name(env: dict[str, str] | None = None) -> str:
    """The sandbox container.

    Both compose files pin `container_name: aiforge`, so this is a constant in
    practice; AIFORGE_CONTAINER exists for a site that renamed it, because
    `aiforge box logs` is the command every error message points at.
    """
    env = os.environ if env is None else env
    return env.get("AIFORGE_CONTAINER") or CONTAINER


def container_state(exe: str) -> str:
    """``running`` / ``exited`` / ``missing`` — the box's own word for it."""
    r = subprocess.run([exe, "inspect", "-f", "{{.State.Status}}", container_name()],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else "missing"


# ── compose file the CLI owns ─────────────────────────────────────────────


def compose_path(cfg: Config, env: dict[str, str] | None = None) -> Path:
    """Next to the approvals file, NOT inside ~/.aiforge.

    ~/.aiforge is bind-mounted into the box, so a compose file written there
    would hand the agent the host's whole mount list and every passthrough
    value — including proxy URLs, which routinely carry credentials.
    """
    return approvals_file(env).parent / "sandbox" / "cli-compose.yml"


def render_compose(cfg: Config, mount_paths: list[str], *,
                   env: dict[str, str] | None = None,
                   plat: str | None = None) -> str:
    """The compose file for a no-repo host.

    Published ports rather than host networking: Docker Desktop on macOS and
    Windows has no usable host network, and a published loopback port is what
    makes the API reachable at 127.0.0.1 on all three platforms. Mounts land at
    the same path on macOS/Linux and under /host/<drive> on Windows, which is
    what paths.to_box() tells the API.
    """
    env = os.environ if env is None else env
    box_home = env.get("AIFORGE_BOX_HOME", "/home/aiforge")
    # Always ":" — the reader is inside the box (sandbox_mounts.mounted()
    # splits on it, as does run.sh), and these are already BOX paths, where the
    # Windows drive colon has been turned into /host/<drive>.
    mount_env = ":".join([_box_config_dir(box_home),
                          *(paths.to_box(m, platform=plat) for m in mount_paths)])
    lines = [
        "# Generated by the aiforge CLI — edit ~/.aiforge/cli.toml, not this file.",
        "services:",
        f"  {SERVICE}:",
        f"    image: {_yaml(cfg.image)}",
        f"    container_name: {CONTAINER}",
        "    restart: unless-stopped",
        "    ports:",
        f"      - {_yaml(f'127.0.0.1:{cfg.port}:{cfg.port}')}",
        "    environment:",
        f"      - {_yaml(f'AIFORGE_RUN_ARGS=--host 0.0.0.0 --port {cfg.port}')}",
        f"      - {_yaml('AIFORGE_MOUNTS=' + mount_env)}",
    ]
    for key in PASSTHROUGH:
        if env.get(key):
            lines.append(f"      - {_yaml(f'{key}={env[key]}')}")
    lines += [
        "    volumes:",
        f"      - {_yaml(f'{cfg.config_dir}:{box_home}/.aiforge')}",
    ]
    for m in mount_paths:
        lines.append(f"      - {_yaml(f'{m}:{paths.to_box(m, platform=plat)}')}")
    lines += [
        "      - aiforge-state:/var/lib/aiforge",
        "volumes:",
        "  aiforge-state:",
    ]
    return "\n".join(lines) + "\n"


def platform_is_windows(plat: str | None = None) -> bool:
    return (plat or os.name) in ("nt", "windows")


def _yaml(value: str) -> str:
    r"""A single-quoted YAML scalar — the one form with no escape sequences.

    A double-quoted scalar interprets backslashes, so `C:\Users\m` contained
    `\U` (a unicode escape) and the file would not parse at all.
    """
    return "'" + value.replace("'", "''") + "'"


def _box_config_dir(box_home: str) -> str:
    return f"{box_home}/.aiforge"


def write_compose(cfg: Config, *, env: dict[str, str] | None = None) -> Path:
    path = compose_path(cfg, env)
    path.parent.mkdir(parents=True, exist_ok=True)
    mount_paths = mountlist.effective(cfg.mounts_file, approvals_file(env))
    path.write_text(render_compose(cfg, mount_paths, env=env))
    with contextlib.suppress(OSError):
        path.chmod(0o600)          # it lists every mounted host folder
    return path


# ── lifecycle ─────────────────────────────────────────────────────────────


def _run_stream(cmd: list[str], cwd: Path | None = None) -> Iterator[str]:
    """Run a command, yielding its output lines as they appear.

    docker's pull progress is the one thing here worth watching live: a cold
    start is a 700 MB download and a silent CLI looks hung.
    """
    proc = subprocess.Popen(cmd, cwd=str(cwd) if cwd else None,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    assert proc.stdout is not None
    for line in proc.stdout:
        yield line.rstrip("\n")
    proc.wait()
    if proc.returncode != 0:
        raise BoxError(f"`{' '.join(cmd[:3])} …` failed (exit {proc.returncode}). "
                       f"See `aiforge box logs`.")


def start(cfg: Config, *, on_line: Callable[[str], None] | None = None,
          recreate: bool = False, env: dict[str, str] | None = None) -> None:
    """Bring the sandbox up. Idempotent; never asks anything."""
    env = os.environ if env is None else env
    exe = require_docker()
    say = on_line or (lambda _s: None)

    if cfg.repo is not None and shutil.which("bash"):
        args = ["bash", str(cfg.repo / "run.sh"), "--port", str(cfg.port), "--skip-web"]
        if recreate:
            subprocess.run(["bash", str(cfg.repo / "run.sh"), "--stop"],
                           cwd=str(cfg.repo), capture_output=True, text=True)
        for line in _run_stream(args, cwd=cfg.repo):
            say(line)
        return

    if not image_present(exe, cfg.image):
        raise BoxError(
            f"the sandbox image `{cfg.image}` is not on this machine and there is no "
            f"AIForge checkout to build it from.\n"
            f"  Either: docker pull {cfg.image}\n"
            f"  or:     set AIFORGE_SANDBOX_IMAGE=<your image>\n"
            f"  or:     clone AIForgeCrew and run ./run.sh once (set AIFORGE_REPO=<path>)."
        )
    compose = write_compose(cfg, env=env)
    cmd = [exe, "compose", "-p", PROJECT, "-f", str(compose), "up", "-d"]
    if recreate:
        cmd.append("--force-recreate")
    for line in _run_stream(cmd):
        say(line)


def stop(cfg: Config) -> None:
    """Stop, not remove: the box keeps whatever the agent installed in it."""
    exe = require_docker()
    if cfg.repo is not None and shutil.which("bash"):
        subprocess.run(["bash", str(cfg.repo / "run.sh"), "--stop"],
                       cwd=str(cfg.repo), capture_output=True, text=True)
        return
    subprocess.run([exe, "compose", "-p", PROJECT, "-f", str(compose_path(cfg)), "stop"],
                   capture_output=True, text=True)  # noqa: S603 — fixed argv


def logs(cfg: Config, *, tail: int = 200, follow: bool = False) -> int:
    exe = require_docker()
    cmd = [exe, "logs", f"--tail={tail}"]
    if follow:
        cmd.append("-f")
    cmd.append(container_name())
    return subprocess.call(cmd)


def shell(cfg: Config) -> int:
    """A shell inside the box, with the user's own tty."""
    exe = require_docker()
    return subprocess.call([exe, "exec", "-it", container_name(), "bash", "-l"])


def wait_healthy(is_healthy: Callable[[], bool], *, timeout: float = 90.0,
                 interval: float = 1.0, on_tick: Callable[[float], None] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> float:
    """Block until the API answers, returning how long that took.

    ``clock``/``sleep`` are injected so the timeout path is testable without
    actually waiting 90 seconds.
    """
    t0 = clock()
    while True:
        if is_healthy():
            return clock() - t0
        waited = clock() - t0
        if waited >= timeout:
            raise BoxError(
                f"the sandbox did not answer on 127.0.0.1 within {timeout:.0f}s.\n"
                f"  Look at why: aiforge box logs --tail 50"
            )
        if on_tick:
            on_tick(waited)
        sleep(interval)
