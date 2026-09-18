"""Starting the sandbox, and knowing why it will not start.

Two strategies, picked by what the host has:

* **repo** — a checkout is present, so ``run.sh`` owns the box. It builds the
  image, generates the mount overlay and knows every env passthrough; the CLI
  would only reimplement it worse.
* **compose** — the `aiforge` binary: no repo, no bash, no python. The binary
  carries the sandbox source (payload.py); the CLI builds the image from it
  the first time (`aiforge install` says so), writes its own compose file from
  the template below and drives ``docker compose`` directly.

Either way the CLI never mounts a folder the host has not approved (see
mounts.py).
"""

from __future__ import annotations

import contextlib
import os
import platform as _platform
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from . import mounts as mountlist
from . import paths, payload
from .config import Config, approvals_file

RUN_SH = "run.sh"            # the repo strategy's entry point
SERVICE = "aiforge"
CONTAINER = "aiforge"

# Environment the box needs and the host may have set (a subset of what
# docker-compose.yml passes through: no token or bind-host knobs — this box is
# loopback-only by construction). Anything unset is simply omitted.
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
    return ("docker is not installed. On Ubuntu: "
            "sudo apt install docker.io docker-compose-v2 && sudo usermod -aG docker $USER "
            "(log out and back in); elsewhere see https://docs.docker.com/engine/install/. "
            "Then re-run aiforge.")


def _daemon_hint() -> str:
    sysname = _platform.system()
    if sysname == "Darwin":
        return "the docker daemon is not running. Start Docker Desktop, then re-run aiforge."
    if sysname == "Windows":
        return "the docker daemon is not running. Start Docker Desktop, then re-run aiforge."
    return ("the docker daemon is not reachable. Try: sudo systemctl start docker "
            "(or `systemctl --user start docker` for rootless), then re-run aiforge.")


# How long `aiforge` waits for a docker daemon it just started (Docker Desktop
# takes ~20–60 s from cold).
DOCKER_START_WAIT_S = 120.0


def _daemon_up(exe: str) -> "tuple[bool, str]":
    try:
        r = subprocess.run([exe, "info", "--format", "{{.ServerVersion}}"],
                           capture_output=True, text=True, timeout=25)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    lines = (r.stderr or "").strip().splitlines()
    return r.returncode == 0, (lines[-1] if lines else "")


# A DOCKER_HOST that is still THIS machine's daemon: the rootless socket Docker's
# own docs tell you to export, Docker Desktop's ~/.docker/run socket, the
# Windows named pipes. Remote hosts and other runtimes' sockets are not ours.
_OTHER_RUNTIMES = ("colima", "orbstack", "/.rd/", "lima", "podman")


def _host_is_local(host: str) -> bool:
    h = host.strip().lower()
    if h.startswith(("tcp://", "ssh://", "http://", "https://")):
        return False
    if h.startswith(("unix://", "npipe://")):
        return not any(r in h for r in _OTHER_RUNTIMES)
    return False


def _docker_target(exe: str, env: "dict[str, str]") -> "tuple[bool, str]":
    """(is it THIS machine's daemon, how to name it). With a remote
    DOCKER_HOST, or a context such as colima / orbstack / a remote host,
    starting Docker Desktop is not ours to do."""
    if env.get("DOCKER_HOST"):
        return _host_is_local(env["DOCKER_HOST"]), f"DOCKER_HOST={env['DOCKER_HOST']}"
    try:
        ctx = subprocess.run([exe, "context", "show"], capture_output=True, text=True,
                             timeout=10, env={**os.environ, **env}).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return True, ""
    local = ctx in ("", "default", "desktop-linux", "desktop-windows", "rootless")
    return local, f"docker context `{ctx}`"


def _launch_daemon(desktop: bool = False) -> bool:
    """Start the docker daemon the way this OS runs it, without sudo: Docker
    Desktop on macOS/Windows (and on Linux when that is the context), else the
    rootless user service on Linux. True when a start was attempted (it may
    still take a while to come up). Never blocks: the rootless unit is
    Type=notify with no start timeout, so a plain `systemctl start` could wait
    forever."""
    sysname = _platform.system()
    try:
        if sysname == "Darwin":
            return subprocess.run(["open", "-g", "-a", "Docker"],
                                  capture_output=True, timeout=20).returncode == 0
        if sysname == "Windows":
            for root in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                         os.environ.get("LOCALAPPDATA", "")):
                exe = os.path.join(root, "Docker", "Docker", "Docker Desktop.exe")
                if root and os.path.exists(exe):
                    subprocess.Popen([exe], close_fds=True)   # noqa: S603 — fixed path
                    return True
            return False
        unit = "docker-desktop" if desktop else "docker"
        return subprocess.run(["systemctl", "--user", "--no-block", "start", unit],
                              capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _timeout_hint() -> str:
    sysname = _platform.system()
    if sysname in ("Darwin", "Windows"):
        return "check that Docker Desktop finished starting"
    return "check `systemctl --user status docker`"


def require_docker(*, launch: bool = False, on_wait: "Callable[[float], None] | None" = None,
                   sleep: "Callable[[float], None]" = time.sleep,
                   clock: "Callable[[], float]" = time.monotonic,
                   env: "dict[str, str] | None" = None) -> str:
    """The docker binary, or a BoxError whose message is the remedy. With
    ``launch``, a stopped LOCAL daemon is started (Docker Desktop / the user
    service) and waited for — by the clock, not by counting sleeps, since one
    `docker info` against a half-started engine can take 25 s."""
    env = os.environ if env is None else env
    exe = docker_bin()
    if not exe:
        raise BoxError(_install_hint())
    up, why = _daemon_up(exe)
    if up:
        return exe
    local, target = _docker_target(exe, env) if launch else (True, "")
    if launch and not local:
        raise BoxError(f"docker at {target} is not answering — start it (it is not "
                       f"this machine's Docker Desktop), then re-run aiforge."
                       + (f" ({why})" if why else ""))
    if launch and _launch_daemon(desktop=target.endswith("`desktop-linux`")):
        t0 = clock()
        while clock() - t0 < DOCKER_START_WAIT_S:
            sleep(2.0)
            if on_wait:
                on_wait(clock() - t0)
            up, why = _daemon_up(exe)
            if up:
                return exe
        raise BoxError(f"docker was started but did not answer within "
                       f"{DOCKER_START_WAIT_S:.0f}s — {_timeout_hint()}, then re-run aiforge."
                       + (f" ({why})" if why else ""))
    raise BoxError(f"{_daemon_hint()} ({why})" if why else _daemon_hint())


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


def project_name(env: dict[str, str] | None = None) -> str:
    """The compose project (`-p`) of the box the CLI owns — named for the
    container, in the only alphabet compose accepts ([a-z0-9_-])."""
    name = re.sub(r"[^a-z0-9_-]+", "-", container_name(env).lower()).strip("-_")
    return name or CONTAINER


def container_status(exe: str, env: dict[str, str] | None = None) -> "tuple[str, int]":
    """(``running`` / ``exited`` / ``restarting`` / ``paused`` / ``missing`` /
    ``unknown``, how many times docker restarted it). ``unknown``: docker did
    not answer in time — a hung daemon must not hang the CLI."""
    try:
        r = subprocess.run([exe, "inspect", "-f", "{{.State.Status}} {{.RestartCount}}",
                            container_name(env)], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return "unknown", 0
    parts = r.stdout.split() if r.returncode == 0 else []
    if not parts:
        return "missing", 0
    return parts[0], int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0


def container_state(exe: str, env: dict[str, str] | None = None) -> str:
    """The box's own word for its state (see container_status)."""
    return container_status(exe, env)[0]


def log_tail(exe: str, env: dict[str, str] | None = None, lines: int = 1) -> str:
    """The box's last log lines (what its first start is busy installing); ""
    when docker cannot say."""
    try:
        r = subprocess.run([exe, "logs", f"--tail={lines}", container_name(env)],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return ((r.stdout or "") + (r.stderr or "")).strip()


# A box's FIRST start installs its dependencies and builds the web UI inside
# (a cold start measured ~7 min): wait while it is working, not a flat 2 min.
READY_WAIT_S = 1800.0


def wait_ready(is_healthy: Callable[[], bool], *, env: dict[str, str] | None = None,
               timeout: float = READY_WAIT_S,
               on_tick: Callable[[float, str], None] | None = None,
               clock: Callable[[], float] = time.monotonic,
               sleep: Callable[[float], None] = time.sleep) -> float:
    """Block until the API answers, for as long as the box is still WORKING on
    it — and fail at once, with its last log lines, if the box stopped or is
    crash-looping, instead of waiting out the clock."""
    exe = docker_bin()
    t0 = clock()
    probed, line = -1e9, ""
    first_restarts: int | None = None
    missing_since: float | None = None
    while True:
        if is_healthy():
            return clock() - t0
        elapsed = clock() - t0
        if elapsed > timeout:
            raise BoxError(f"the sandbox did not answer within {timeout / 60:.0f} min. "
                           f"See: aiforge box logs --tail 50")
        if exe and elapsed - probed >= 5.0:
            probed = elapsed
            state, restarts = container_status(exe, env)
            if first_restarts is None and state not in ("unknown", "missing"):
                first_restarts = restarts       # a real reading, not a timeout's 0
            # docker's restart backoff starts at 100 ms, so a crash loop is
            # mostly sampled as "running": count the restarts instead.
            looping = state == "restarting" or (
                first_restarts is not None and restarts - first_restarts >= 2)
            if state in ("exited", "dead", "paused") or looping:
                why = {"paused": "is paused"}.get(state, "keeps restarting" if looping
                                                  else "stopped")
                raise BoxError(f"the sandbox {why} while starting. Its last lines:\n"
                               f"{log_tail(exe, env, 15)}\n  aiforge box logs --tail 50")
            if state == "missing":
                missing_since = elapsed if missing_since is None else missing_since
                if elapsed - missing_since >= 60.0:
                    raise BoxError(f"there is no container named `{container_name(env)}` "
                                   f"(AIFORGE_CONTAINER?). See: aiforge box status")
            else:
                missing_since = None
            with contextlib.suppress(Exception):
                line = log_tail(exe, env).splitlines()[-1][:100]
        if on_tick:
            on_tick(elapsed, line)
        sleep(1.0)


# ── compose file the CLI owns ─────────────────────────────────────────────


def compose_path(env: dict[str, str] | None = None) -> Path:
    """Next to the approvals file, NOT inside ~/.aiforge.

    ~/.aiforge is bind-mounted into the box, so a compose file written there
    would hand the agent the host's whole mount list and every passthrough
    value — including proxy URLs, which routinely carry credentials.
    """
    return approvals_file(env).parent / "sandbox" / "cli-compose.yml"


def render_compose(cfg: Config, mount_paths: list[str], *,
                   env: dict[str, str] | None = None,
                   plat: str | None = None, host_net: bool = False) -> str:
    """The compose file for a no-repo host.

    ``host_net`` (native Linux docker): the host's network, the API bound to
    127.0.0.1 — exactly what docker-compose.yml does for run.sh. Loopback-only
    for real, and a model server on the host is at 127.0.0.1 as usual.

    Otherwise (Docker Desktop, rootless docker) a port published on the host's
    127.0.0.1. There the container's own address is inside a VM / a private
    namespace, which nothing else can route to. NOT used on native rootful
    Linux: docker before 28 lets anyone on the LAN segment reach a container's
    IP directly, whatever address the port was published on (moby#45610) —
    and this API runs shells with no token. The model server on the host is
    `host.docker.internal` there.

    Mounts land at the same path on macOS/Linux and under /host/<drive> on
    Windows, which is what paths.to_box() tells the API.
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
        f"    container_name: {container_name(env)}",
        "    restart: unless-stopped",
    ]
    if host_net:
        lines += [
            "    network_mode: host",
            "    environment:",
            f"      - {_yaml(f'AIFORGE_RUN_ARGS=--host 127.0.0.1 --port {cfg.port}')}",
        ]
    else:
        lines += [
            "    ports:",
            f"      - {_yaml(f'127.0.0.1:{cfg.port}:{cfg.port}')}",
            "    extra_hosts:",
            f"      - {_yaml('host.docker.internal:host-gateway')}",
            "    environment:",
            f"      - {_yaml(f'AIFORGE_RUN_ARGS=--host 0.0.0.0 --port {cfg.port}')}",
            # 0.0.0.0 is INSIDE the container, whose address nothing outside
            # the VM / namespace can route to (see above): the only way in is
            # the port on the host's 127.0.0.1. Without this the API's boot
            # guard refuses a non-loopback bind with no token and crash-loops.
            f"      - {_yaml('AIFORGE_ALLOW_UNAUTH_NONLOOPBACK=1')}",
        ]
    lines.append(f"      - {_yaml('AIFORGE_MOUNTS=' + mount_env)}")
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


def write_compose(cfg: Config, *, env: dict[str, str] | None = None,
                  host_net: bool = False) -> Path:
    path = compose_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create ~/.aiforge as YOU before it is mounted: a bind-mount source that
    # does not exist is created by the docker daemon, as root — and the app,
    # which runs as your uid inside the box, then cannot open its own database
    # (every chat answered 500 on a fresh machine).
    cfg.config_dir.mkdir(parents=True, exist_ok=True)
    mount_paths = mountlist.effective(cfg.mounts_file, approvals_file(env))
    path.write_text(render_compose(cfg, mount_paths, env=env, host_net=host_net))
    with contextlib.suppress(OSError):
        path.chmod(0o600)          # it lists every mounted host folder
    return path


_REMOTE_SCHEMES = ("tcp://", "ssh://", "http://", "https://")
# tcp://localhost:2375 is still this machine (WSL1, Docker Desktop's "expose
# daemon on tcp://localhost:2375").
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")


def _is_remote(endpoint: str) -> bool:
    e = endpoint.strip().lower()
    if e.startswith("ssh://"):
        return True
    if not e.startswith(_REMOTE_SCHEMES):
        return False                         # unix:// and npipe:// are local
    host = e.split("://", 1)[1].split("/", 1)[0]
    if not host.endswith("]"):
        host = host.rsplit(":", 1)[0]        # drop the port
    return host not in _LOCAL_HOSTS


def _daemon_endpoint(exe: str, env: "dict[str, str]") -> str:
    """Where the docker CLI sends requests: DOCKER_HOST, else the context's."""
    if env.get("DOCKER_HOST"):
        return env["DOCKER_HOST"]
    try:
        r = subprocess.run([exe, "context", "inspect", "--format",
                            "{{.Endpoints.docker.Host}}"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def daemon_kind(exe: str, env: "dict[str, str] | None" = None) -> str:
    """``native`` (a rootful Linux daemon on THIS machine), ``rootless``, or
    ``desktop`` (the daemon is in a local VM: Docker Desktop on any OS, or
    colima / orbstack / Rancher on a Mac or Windows). A daemon on ANOTHER
    machine is refused: the API would be published on that machine, with no
    token, and this CLI could not reach it at 127.0.0.1 anyway."""
    env = os.environ if env is None else env
    endpoint = _daemon_endpoint(exe, env)
    if _is_remote(endpoint):
        raise BoxError(f"docker here talks to another machine ({endpoint}). The sandbox "
                       f"has to run on THIS machine's docker: unset DOCKER_HOST or "
                       f"`docker context use default`, then re-run aiforge.")
    try:
        r = subprocess.run([exe, "info", "--format",
                            "{{.OperatingSystem}}|{{json .SecurityOptions}}"],
                           capture_output=True, text=True, timeout=25)
        os_name, _, sec = (r.stdout if r.returncode == 0 else "").partition("|")
    except (OSError, subprocess.SubprocessError):
        os_name, sec = "", ""
    if "rootless" in sec:
        return "rootless"
    # A local daemon on a Mac or a Windows box runs in a VM whatever it calls
    # itself; on Linux only Docker Desktop does.
    if "Docker Desktop" in os_name or _platform.system() != "Linux":
        return "desktop"
    return "native"


def foreign_project(exe: str, env: "dict[str, str] | None" = None) -> str:
    """The compose project of a container with our name that is NOT ours (a
    checkout's ./run.sh started it); "" when there is none, or it is ours."""
    try:
        r = subprocess.run([exe, "inspect", "-f",
                            '{{index .Config.Labels "com.docker.compose.project"}}',
                            container_name(env)], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return ""
    owner = r.stdout.strip() if r.returncode == 0 else ""
    return owner if owner and owner != project_name(env) else ""


def _refuse_a_foreign_box(exe: str, env: "dict[str, str]") -> None:
    """A container with our name from ANOTHER compose project would only
    collide with ours on `compose up`."""
    owner = foreign_project(exe, env)
    if owner:
        name = container_name(env)
        raise BoxError(f"the sandbox `{name}` on this machine was started from a checkout "
                       f"(compose project `{owner}`), not by this aiforge. Use ./run.sh "
                       f"there, or remove it first: ./run.sh --stop && docker rm {name}")


def running_image(exe: str, env: "dict[str, str] | None" = None) -> str:
    """The image the sandbox container was created from; "" when there is none."""
    try:
        r = subprocess.run([exe, "inspect", "-f", "{{.Config.Image}}", container_name(env)],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def require_compose(exe: str) -> None:
    """Checked BEFORE a minutes-long image build, not after it."""
    try:
        ok = subprocess.run([exe, "compose", "version"], capture_output=True,
                            text=True, timeout=25).returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    if not ok:
        raise BoxError("docker is here but `docker compose` (v2) is not. On Ubuntu: "
                       "sudo apt install docker-compose-v2; with Docker's own repo "
                       "(Debian, Fedora, RHEL): docker-compose-plugin. Docker Desktop "
                       "includes it. Then re-run aiforge.")


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


def start_lock_path(env: dict[str, str] | None = None) -> Path:
    return approvals_file(env).parent / "sandbox" / ".start.lock"


@contextlib.contextmanager
def start_lock(env: dict[str, str] | None = None, *, wait: bool = False):
    """Serialise sandbox creation across terminals.

    Every connection shares ONE box, and a second `aiforge` only touches docker
    when the API does not answer — but two cold starts at the same moment both
    run `compose up`, and the loser gets `Conflict. The container name
    "/aiforge" is already in use`. The holder creates the box; anyone else
    waits for it instead of racing (yields False).

    POSIX only: flock is what makes this cheap and automatically released when
    the process dies. On Windows the lock is skipped rather than faked.
    """
    path = start_lock_path(env)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = path.open("a+")
    except OSError:
        yield True                       # no lock file, no serialisation
        return
    try:
        import fcntl
    except ImportError:                  # pragma: no cover - Windows
        fh.close()
        yield True
        return
    try:
        fcntl.flock(fh, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        yield False                      # somebody else is starting it
        return
    try:
        yield True
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()


def start(cfg: Config, *, on_line: Callable[[str], None] | None = None,
          recreate: bool = False, env: dict[str, str] | None = None) -> None:
    """Bring the sandbox up — and docker itself when it is stopped. Idempotent;
    never asks anything."""
    env = os.environ if env is None else env
    say = on_line or (lambda _s: None)
    exe = require_docker(launch=True, env=env,
                         on_wait=lambda s: say(f"waiting for docker to start… {s:.0f}s"))

    if cfg.repo is not None and shutil.which("bash"):
        # No --skip-web: run.sh forwards it into the box, which then never built
        # the web UI — a box started from the CLI had the API and no /ui/.
        # The build runs once; later starts see it is up to date.
        args = ["bash", str(cfg.repo / RUN_SH), "--port", str(cfg.port)]
        if recreate:
            subprocess.run(["bash", str(cfg.repo / RUN_SH), "--stop"],
                           cwd=str(cfg.repo), capture_output=True, text=True)
        for line in _run_stream(args, cwd=cfg.repo):
            say(line)
        return

    require_compose(exe)
    kind = daemon_kind(exe, env)
    _refuse_a_foreign_box(exe, env)
    if not image_present(exe, cfg.image):
        carried = payload.tarball()
        if carried is None:
            raise BoxError(
                f"the sandbox image `{cfg.image}` is not on this machine and there is no "
                f"AIForge source to build it from.\n"
                f"  Either: download the aiforge binary (it carries the source) and run "
                f"`aiforge install`\n"
                f"  or:     docker pull {cfg.image}  /  set AIFORGE_SANDBOX_IMAGE=<your image>\n"
                f"  or:     clone AIForgeCrew and run ./run.sh (or aiforge) inside it."
            )
        if cfg.image != payload.image_tag(carried):
            raise BoxError(
                f"the sandbox image is set to `{cfg.image}` (AIFORGE_SANDBOX_IMAGE, or "
                f"image= in ~/.aiforge/cli.toml), and it is not on this machine.\n"
                f"  Unset it to build from the source this binary carries, or: "
                f"docker pull {cfg.image}")
        build_image(exe, cfg.image, payload.extract(compose_path(env).parent, carried),
                    say=say, env=env, rootless=kind == "rootless")
    compose = write_compose(cfg, env=env, host_net=kind == "native")
    cmd = [exe, "compose", "-p", project_name(env), "-f", str(compose), "up", "-d"]
    if recreate:
        cmd.append("--force-recreate")
    for line in _run_stream(cmd):
        say(line)


def _identity() -> "tuple[int, int, str]":
    """Your uid/gid/name, so files the agent writes in mounted folders are
    yours. Windows has none: Docker Desktop maps ownership itself."""
    if hasattr(os, "getuid"):
        import pwd
        uid = os.getuid()
        try:
            name = pwd.getpwuid(uid).pw_name
        except KeyError:
            name = "aiforge"
        return uid, os.getgid(), name
    return 1000, 1000, "aiforge"


def build_image(exe: str, image: str, src: Path, *,
                say: Callable[[str], None], env: dict[str, str] | None = None,
                rootless: bool = False) -> None:
    """`docker build` the sandbox image from the unpacked source — the same
    Dockerfile and build args docker-compose.yml gives it. The first time takes
    a few minutes (Ubuntu base + apt); later versions reuse docker's layer cache.

    Rootless docker maps YOUR uid to root inside the box, so the app runs as
    uid 0 there (the entrypoint handles it) — any other uid is a subuid that
    cannot write your ~/.aiforge."""
    env = os.environ if env is None else env
    uid, gid, name = (0, 0, "root") if rootless else _identity()
    args = {"APP_UID": str(uid), "APP_GID": str(gid), "APP_USER": name,
            # Where the compose file mounts ~/.aiforge: the app's HOME.
            "APP_HOME": env.get("AIFORGE_BOX_HOME", "/home/aiforge"),
            "APT_MIRROR": env.get("AIFORGE_APT_MIRROR", ""),
            "BASE_REGISTRY": env.get("AIFORGE_BASE_REGISTRY", "")}
    cmd = [exe, "build", "-t", image, "--network", "host"]
    for k, v in args.items():
        cmd += ["--build-arg", f"{k}={v}"]
    say("building the sandbox image (first time: a few minutes)…")
    for line in _run_stream([*cmd, str(src)]):
        say(line)


def stop(cfg: Config, env: dict[str, str] | None = None) -> bool:
    """Stop, not remove: the box keeps whatever the agent installed in it.
    True when no sandbox container is left running."""
    env = os.environ if env is None else env
    exe = require_docker()
    if cfg.repo is not None and shutil.which("bash"):
        subprocess.run(["bash", str(cfg.repo / RUN_SH), "--stop"],
                       cwd=str(cfg.repo), capture_output=True, text=True)
    elif compose_path(env).is_file():
        subprocess.run([exe, "compose", "-p", project_name(env), "-f", str(compose_path(env)),
                        "stop"], capture_output=True, text=True)  # noqa: S603 — fixed argv
    # Only a state that is really not serving counts: "unknown" is a docker
    # that did not answer, "restarting" is still the box.
    return container_state(exe, env) not in ("running", "restarting", "unknown")


def logs(*, tail: int = 200, follow: bool = False) -> int:
    """`docker logs` on the sandbox. Takes no config: the container is named,
    not derived from it (AIFORGE_CONTAINER overrides)."""
    exe = require_docker()
    cmd = [exe, "logs", f"--tail={tail}"]
    if follow:
        cmd.append("-f")
    cmd.append(container_name())
    return subprocess.call(cmd)


def shell() -> int:
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
