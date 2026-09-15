"""Where the CLI keeps its own state, and how it finds the API.

Local only, by design: the backend trusts loopback, so there is no token, no
remote URL and no auth code path in this client. What is configurable is the
port, the sandbox image, and where the repo is if this host has one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PORT = 8799


def config_dir(env: dict[str, str] | None = None) -> Path:
    """``~/.aiforge`` — shared with the sandbox, which mounts it."""
    env = os.environ if env is None else env
    return Path(env.get("AIFORGE_CONFIG_DIR") or (Path.home() / ".aiforge"))


def approvals_file(env: dict[str, str] | None = None) -> Path:
    """The host's mount approvals, kept OUTSIDE ~/.aiforge on purpose.

    ~/.aiforge is mounted into the box, so the agent can append to mounts.list.
    An approval is the host saying yes to one of those lines, so it has to live
    somewhere the box cannot reach — same file run.sh uses.
    """
    env = os.environ if env is None else env
    base = env.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "aiforge" / "approved-mounts"


@dataclass(frozen=True)
class Config:
    port: int
    config_dir: Path
    repo: Path | None          # a checkout, when this host has one
    image: str                 # sandbox image tag
    auto_mount: bool           # answer the one mount prompt with yes
    verbosity: int             # -1 quiet, 0 normal, 1 verbose
    json_events: bool          # dump raw events instead of rendering

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def sessions_file(self) -> Path:
        return self.config_dir / "cli-sessions.json"

    @property
    def history_file(self) -> Path:
        return self.config_dir / "cli-history"

    @property
    def mounts_file(self) -> Path:
        return self.config_dir / "mounts.list"


def _toml(path: Path) -> dict:
    """``cli.toml`` if it parses, else nothing. A broken config must not stop
    the CLI from starting — it falls back to defaults and says so nowhere,
    because every value it holds is also a flag."""
    try:
        import tomllib
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        return data.get("cli", data) if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — missing, unreadable or malformed
        return {}


def _find_repo(env: dict[str, str], cwd: Path) -> Path | None:
    """A checkout of AIForgeCrew, if this host has one.

    With a repo, box lifecycle goes through its run.sh — the maintained path
    that builds the image and generates the mount overlay. Without one (the
    normal case for someone who installed a binary) the CLI drives docker
    compose itself against a prebuilt image.
    """
    cand = []
    if env.get("AIFORGE_REPO"):
        cand.append(Path(env["AIFORGE_REPO"]))
    cand += [cwd, *cwd.parents, Path.home() / "AIForgeCrew"]
    for c in cand:
        try:
            if (c / "run.sh").is_file() and (c / "docker-compose.yml").is_file():
                return c
        except OSError:
            continue
    return None


def load(args=None, env: dict[str, str] | None = None, cwd: Path | None = None) -> Config:
    """Flags beat env beats cli.toml beats defaults."""
    env = os.environ if env is None else env
    cwd = Path.cwd() if cwd is None else cwd
    cfg_dir = config_dir(env)
    file = _toml(cfg_dir / "cli.toml")

    def pick(flag, env_key, file_key, default):
        if flag is not None:
            return flag
        if env.get(env_key):
            return env[env_key]
        if file.get(file_key) is not None:
            return file[file_key]
        return default

    port = pick(getattr(args, "port", None), "AIFORGE_CLI_PORT", "port", DEFAULT_PORT)
    image = pick(None, "AIFORGE_SANDBOX_IMAGE", "image", "aiforge-sandbox:local")
    auto = bool(pick(getattr(args, "yes", None) or None, "AIFORGE_CLI_AUTO_MOUNT",
                     "auto_mount", False))
    return Config(
        port=int(port),
        config_dir=cfg_dir,
        repo=_find_repo(env, cwd),
        image=str(image),
        auto_mount=auto in (True, "1", "true", "yes"),
        verbosity=int(getattr(args, "verbosity", 0) or 0),
        json_events=bool(getattr(args, "json_events", False)),
    )
